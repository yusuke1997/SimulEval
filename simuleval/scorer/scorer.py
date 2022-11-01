# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import shutil
import functools
from argparse import Namespace
from collections import defaultdict
import textgrid
import subprocess
from typing import Dict, Generator, List, Optional, Union
import sacrebleu
from .instance import INSTANCE_TYPE_DICT, eval_all_latency, LogInstance
import os
import sys
import logging
import json
from statistics import mean
from pathlib import Path
from simuleval.utils.common import load_fairseq_manifest, get_fairseq_manifest_path
from simuleval.data.dataloader import GenericDataloader

logger = logging.getLogger("simuleval.sentence_level_scorer")


class SentenceLevelScorer(object):
    def __init__(
        self,
        dataloader: Optional[GenericDataloader],
        args: Namespace,
        reset: bool = True,
    ) -> None:
        self.dataloader = dataloader
        self.instances = {}
        self.sacrebleu_tokenizer = "13a"
        self.reference_list = None
        self.source_lengths = None

        if args is not None:
            self.start_index = args.start_index
            self.end_index = args.end_index
            if self.end_index < 0:
                self.end_index = len(self.dataloader)
            self.args = args
            # self.eval_latency_unit = args.eval_latency_unit
            # self.sacrebleu_tokenizer = args.sacrebleu_tokenizer
            # self.no_space = args.no_space
            self.output = Path(args.output)

            self.instance_class = INSTANCE_TYPE_DICT[
                f"{args.source_type}-{args.target_type}"
            ]

        if reset:
            self.reset()

    def __len__(self) -> int:
        return self.end_index - self.start_index

    def get_indices(self) -> Generator:
        for index in range(self.start_index, self.end_index):
            yield index

    def get_info(self) -> Dict[str, int]:
        return {"num_sentences": len(self)}

    def send_source(self, instance_id: int, segment_size: int) -> Dict:
        dict_to_return = self.instances[instance_id].send_source(
            segment_size=segment_size
        )
        dict_to_return["instance_id"] = instance_id
        return dict_to_return

    def reset(self) -> None:
        if len(self.instances) > 0:
            logger.warning("Resetting scorer")

        for i in self.get_indices():
            self.instances[i] = self.instance_class(i, self.dataloader, self.args)

    def get_translation_list(self) -> List[str]:
        raise NotImplementedError

    def get_reference_list(self) -> List[str]:
        return [self.instances[i].reference for i in self.get_indices()]

    def get_quality_score(self) -> Dict[str, float]:
        bleu_score = sacrebleu.corpus_bleu(
            self.get_translation_list(),
            [self.get_reference_list()],
            tokenize=self.sacrebleu_tokenizer,
        ).score
        return {"BLEU": bleu_score}

    def get_latency_score(self) -> Dict[str, Dict[str, float]]:
        common_keys = functools.reduce(
            lambda x, y: x.intersection(y),
            (set(x.metrics.keys()) for x in self.instances.values()),
        )

        results = {}
        for metric in ["AL", "AP", "DAL"]:
            results[metric] = mean(
                [seg.metrics["latency"][metric] for seg in self.instances.values()]
            )
            if "latency_ca" in common_keys:
                results[metric + "_CA"] = mean(
                    [
                        seg.metrics["latency_ca"][metric]
                        for seg in self.instances.values()
                    ]
                )

            if "latency_text_w_time" in common_keys:
                results[metric + " (Time in ms)"] = mean(
                    [
                        seg.metrics["latency_text_w_time"][metric]
                        for seg in self.instances.values()
                    ]
                )

        return results

    def score(self):
        return {
            "Quality": self.get_quality_score(),
            "Latency": self.get_latency_score(),
        }


class SentenceLevelTextScorer(SentenceLevelScorer):
    def get_translation_list(self) -> List[str]:
        not_finish_write_id = [
            i for i in self.get_indices() if not self.instances[i].finish_prediction
        ]
        empty_hypo_id = [
            str(i) for i in self.get_indices() if len(self.instances[i].prediction) == 0
        ]

        if len(not_finish_write_id) > 0:
            logger.warn(
                "Warning: these hypothesis don't have EOS in predictions",
            )
            logger.warn(", ".join((str(x) for x in not_finish_write_id)))
            for idx in not_finish_write_id:
                self.instances[idx].sentence_level_eval()

        if len(empty_hypo_id) > 0:
            logger.warn("Warning: these hypothesis are empty")
            logger.warn(", ".join(empty_hypo_id))

        translations = [self.instances[i].prediction for i in self.get_indices()]

        return translations

    @classmethod
    def from_logdir(cls, logdir: Union[Path, str]):
        logdir = Path(logdir)
        instances = {}

        instance_class = INSTANCE_TYPE_DICT["text-text"]

        with open(logdir / "instances.log", "r") as f:
            for line in f:
                instance = instance_class.from_json(line.strip())
                instances[instance.index] = instance
        scorer = cls(None, None, False)
        scorer.start_index = 0
        scorer.end_index = len(instances.keys())
        scorer.instances = instances
        return scorer


class SentenceLevelSpeechScorer(SentenceLevelScorer):
    def __init__(
        self,
        dataloader: Optional[GenericDataloader],
        args: Namespace,
        reset: bool = True,
    ) -> None:
        super().__init__(dataloader, args, reset)
        self.pre_wavs_dir = self.output / "wavs"
        self.pre_wavs_dir.mkdir(exist_ok=True)

    def get_translation_list(self) -> List[str]:
        logger.warn("Beta feature: Evaluating speech output")
        try:
            from ust_common.evaluation import prepare_w2v_audio_finetuning_data
            from ust_common.evaluation import fairseq_w2v_ctc_infer
        except:
            logger.warn("Please install ust_common.")
            return ["" for _ in range(len(self))]

        # TODO make it configurable
        prepare_w2v_audio_finetuning_data(
            self.pre_wavs_dir, self.output / "asr_prep_data", output_subset_name="eval"
        )
        fairseq_w2v_ctc_infer(
            self.output / "asr_prep_data",
            "/checkpoint/annl/s2st/eval/asr/model/wav2vec2/wav2vec_vox_960h_pl.pt",
            "eval",
            self.output / "asr_out",
        )

        translations_w_id = load_fairseq_manifest(
            self.output / "asr_out" / "eval_asr_predictions.tsv"
        )
        translations_w_id = sorted(
            translations_w_id, key=lambda x: int(x["id"].split("_")[-1])
        )

        translation_list = []
        for idx, item in enumerate(translations_w_id):
            with open(self.pre_wavs_dir / f"{idx}_pred.txt", "w") as f:
                f.write(item["transcription"].lower() + "\n")
            translation_list.append(item["transcription"].lower())

        return translation_list

    def get_source_lengths(self) -> List[float]:
        if self.source_lengths is None:
            self.source_lengths = [seg.source_length for seg in self.instances.values()]
        return self.source_lengths

    def get_reference_list(self) -> List[str]:
        if self.reference_list is not None:
            return self.reference_list
        if len(self.instances.keys()) > 0:
            return super().get_reference_list()
        else:
            refer_list = []
            src_len_list = []
            with open(self.output / "instances.log", "r") as f:
                for line in f:
                    refer_list.append(json.loads(line.strip())["reference"])
                    src_len_list.append(json.loads(line.strip())["source_length"])
            self.reference_list = refer_list
            self.source_lengths = src_len_list
            return self.reference_list

    def prepare_alignment(self):
        try:
            subprocess.run("which mfa", shell=True, check=True)
        except:
            logger.error("Please make sure the mfa is correctly installed.")
            sys.exit(1)
        logger.info("Align target transcripts with speech.")
        temp_dir = Path(self.output) / "mfa"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(exist_ok=True)
        original_model_path = Path.home() / "Documents/MFA/pretrained_models"
        acoustic_model_path = temp_dir / "acoustic.zip"
        acoustic_model_path.symlink_to(
            original_model_path / "acoustic" / "english_mfa.zip"
        )
        dictionary_path = temp_dir / "dict"
        dictionary_path.symlink_to(
            original_model_path / "dictionary" / "english_mfa.dict"
        )
        mfa_command = f"mfa align {self.output / 'wavs'} {dictionary_path.as_posix()} {acoustic_model_path.as_posix()} {self.output / 'align'} --clean --overwrite --temporary_directory  {temp_dir.as_posix()}"
        logger.info(mfa_command)

        subprocess.run(
            mfa_command,
            shell=True,
            check=True,
        )

    def get_latency_score(self) -> Dict[str, Dict[str, float]]:
        self.prepare_alignment()

        alignment_dir = self.output / "align"
        delays = dict()
        for file in alignment_dir.iterdir():
            if file.name.endswith("TextGrid"):
                index = int(file.name.split("_")[0])
                target_offset = self.instances[index].summarize()["delays"][0][1]

                info = textgrid.TextGrid.fromFile(file)

                delays[index] = defaultdict(list)
                for interval in info[0]:
                    if len(interval.mark) > 0:
                        delays[index]["BOW"].append(
                            target_offset + 1000 * interval.minTime
                        )
                        delays[index]["EOW"].append(
                            target_offset + 1000 * interval.maxTime
                        )
                        delays[index]["COW"].append(
                            target_offset
                            + 0.5 * (interval.maxTime + interval.minTime) * 1000
                        )

        results = defaultdict(list)
        for index, d in sorted(delays.items()):
            for key, value in d.items():
                results[key].append(
                    eval_all_latency(
                        value,
                        self.get_source_lengths()[index],
                        len(self.get_reference_list()[index].split()),
                    )  # TODO make is configurable
                )
        final_results = defaultdict(dict)
        for key, value in results.items():
            for kk in value[0].keys():
                final_results[key][kk] = mean([item[kk] for item in value])

        return final_results

    @classmethod
    def from_logdir(cls, logdir: Union[Path, str], target_type: str = "text"):
        logdir = Path(logdir)
        instances = {}

        with open(logdir / "instances.log") as f:
            for line in f:
                info = json.loads(line.strip())
                instances[info["index"]] = LogInstance(info)

        args = Namespace(
            output=logdir,
            start_index=0,
            end_index=sum(1 for line in open(logdir / "instances.log")),
            source_type="speech",
            target_type="speech",
        )
        scorer = cls(None, args, False)
        scorer.instances = instances
        return scorer


def compute_score(logdir: Union[Path, str]):
    logdir = Path(logdir)
    if (logdir / "wavs").exists():
        scorer = SentenceLevelSpeechScorer.from_logdir(logdir)
    else:
        scorer = SentenceLevelTextScorer.from_logdir(logdir)

    with open(logdir / "scores", "w") as f:
        f.writelines(json.dumps(scorer.score(), indent=4))

    print(json.dumps(scorer.score(), indent=4))
