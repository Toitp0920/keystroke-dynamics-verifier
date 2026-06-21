from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


SEQUENCE_LENGTH = 100
INPUT_FEATURES = 5
MAX_MILLISECONDS = 1000.0
CROP_VALUE = 30.0

INT_MIN = -2147483648
SIMPAC_MAX_CONTEXT_ORDER = 7
SIMPAC_MIN_OBSERVATIONS = 10
SIMPAC_PARTITION_THRESHOLD_MS = 1500

CACHE_VERSION = 1


@dataclass(frozen=True)
class RawKeystroke:
    participant_id: str
    test_section_id: str
    press_time: int
    release_time: int
    keycode: int
    letter: str = ""


@dataclass(frozen=True)
class TemplateMeta:
    participant_id: str
    source_file: str
    section_id: str
    event_count: int
    language: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "source_file": self.source_file,
            "section_id": self.section_id,
            "event_count": self.event_count,
            "language": self.language,
        }


class BaselineNotFoundError(FileNotFoundError):
    pass


class ModelNotLoadedError(RuntimeError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_cache_stem(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe or "unknown"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _source_file_record(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _file_sha256(path),
    }


def _records_match(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]]) -> bool:
    if len(a) != len(b):
        return False

    a_sorted = sorted(a, key=lambda x: x["path"])
    b_sorted = sorted(b, key=lambda x: x["path"])
    keys = ("path", "size", "mtime_ns", "sha256")
    for left, right in zip(a_sorted, b_sorted):
        for key in keys:
            if left.get(key) != right.get(key):
                return False
    return True


def parse_baseline_filename(path: Path) -> Optional[Dict[str, str]]:
    match = re.match(r"^keystrokes_(?P<language>EN|ZH)_(?P<user_id>.+)_(?P<timestamp>[^_]+)\.tsv$", path.name)
    if not match:
        return None
    return match.groupdict()


def read_tsv_keystrokes(path: Path) -> Dict[str, List[RawKeystroke]]:
    sessions: Dict[str, List[RawKeystroke]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        required = {"PARTICIPANT_ID", "TEST_SECTION_ID", "PRESS_TIME", "RELEASE_TIME", "KEYCODE"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path} is missing required TSV columns: {sorted(required)}")

        for row in reader:
            try:
                section_id = str(row["TEST_SECTION_ID"])
                event = RawKeystroke(
                    participant_id=str(row["PARTICIPANT_ID"]),
                    test_section_id=section_id,
                    press_time=int(float(row["PRESS_TIME"])),
                    release_time=int(float(row["RELEASE_TIME"])),
                    keycode=int(float(row["KEYCODE"])),
                    letter=str(row.get("LETTER", "")),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid keystroke row in {path}: {row}") from exc

            sessions.setdefault(section_id, []).append(event)

    for events in sessions.values():
        events.sort(key=lambda e: (e.press_time, e.release_time, e.keycode))
    return sessions


def events_to_base_tensor(events: Sequence[RawKeystroke]) -> np.ndarray:
    matrix = np.zeros((SEQUENCE_LENGTH, 3), dtype=np.float32)
    max_pos = min(len(events), SEQUENCE_LENGTH)

    for i in range(max_pos):
        event = events[i]
        if 0 <= event.keycode <= 255:
            matrix[i, 0] = event.keycode / 255.0
        else:
            matrix[i, 0] = 0.0

        hold = (event.release_time - event.press_time) / MAX_MILLISECONDS
        if hold < 0.0:
            hold = 0.0
        elif hold > CROP_VALUE:
            hold = CROP_VALUE
        matrix[i, 1] = hold

        if i == 0:
            flight = 0.0
        else:
            flight = (event.press_time - events[i - 1].press_time) / MAX_MILLISECONDS
            if flight < 0.0:
                flight = 0.0
            elif flight > CROP_VALUE:
                flight = CROP_VALUE
        matrix[i, 2] = flight

    return matrix


def _clean_simpac_features(hts: np.ndarray, fts: np.ndarray) -> Tuple[np.ndarray, np.ndarray, List[int]]:
    hts = hts.astype(np.int64, copy=True)
    fts = fts.astype(np.int64, copy=True)
    partition_offsets = [int(i) for i, value in enumerate(fts) if value > SIMPAC_PARTITION_THRESHOLD_MS]

    for i in range(len(fts)):
        if fts[i] < 0:
            fts[i] = np.iinfo(np.int32).max
        if fts[i] > SIMPAC_PARTITION_THRESHOLD_MS:
            fts[i] = SIMPAC_PARTITION_THRESHOLD_MS

        if hts[i] < 0:
            hts[i] = np.iinfo(np.int32).max
        if hts[i] > SIMPAC_PARTITION_THRESHOLD_MS:
            hts[i] = SIMPAC_PARTITION_THRESHOLD_MS

    if len(fts):
        fts[0] = INT_MIN
    for offset in partition_offsets:
        fts[offset] = INT_MIN

    return hts, fts, partition_offsets


@dataclass
class SimpacSample:
    vks: np.ndarray
    hts: np.ndarray
    fts: np.ndarray
    partition_offsets: List[int]


def tensor3_to_simpac_sample(base_tensor: np.ndarray, apply_cleaning: bool = False) -> SimpacSample:
    if base_tensor.shape != (SEQUENCE_LENGTH, 3):
        raise ValueError(f"Expected a (100, 3) base tensor, got {base_tensor.shape}")

    vks = np.rint(base_tensor[:, 0] * 255.0).astype(np.uint8)
    hts = np.rint(base_tensor[:, 1] * MAX_MILLISECONDS).astype(np.int64)
    fts = np.rint(base_tensor[:, 2] * MAX_MILLISECONDS).astype(np.int64)

    if apply_cleaning:
        hts, fts, partition_offsets = _clean_simpac_features(hts, fts)
    else:
        partition_offsets = []
        if len(fts):
            fts[0] = INT_MIN

    return SimpacSample(vks=vks, hts=hts, fts=fts, partition_offsets=partition_offsets)


class HistogramProfile:
    """Python port of the SIMPAC Profile + HistogramSynthesizer path used for 5D features."""

    def __init__(
        self,
        max_context_order: int = SIMPAC_MAX_CONTEXT_ORDER,
        min_observations: int = SIMPAC_MIN_OBSERVATIONS,
        random_seed: Optional[int] = None,
    ) -> None:
        self.max_context_order = max_context_order
        self.min_observations = min_observations
        self.rng = np.random.default_rng(random_seed)
        self.models: Dict[str, List[Dict[int, List[int]]]] = {
            "HT": [dict() for _ in range(max_context_order + 1)],
            "FT": [dict() for _ in range(max_context_order + 1)],
        }

    def build(self, samples: Sequence[SimpacSample]) -> None:
        for sample in samples:
            self.feed(sample)

    def feed(self, sample: SimpacSample) -> None:
        partition_offsets = set(sample.partition_offsets)
        context_order = 1
        context = 0xFF

        for i, vk in enumerate(sample.vks):
            if i in partition_offsets:
                context_order = 1
                context = 0xFF

            current_context_mask = 0
            max_order = min(context_order, self.max_context_order)
            for order in range(max_order + 1):
                current_context = context & current_context_mask
                model_hash = int(current_context | (int(vk) << 56))

                self._feed_feature("HT", order, model_hash, int(sample.hts[i]))
                self._feed_feature("FT", order, model_hash, int(sample.fts[i]))

                current_context_mask = (current_context_mask << 8) | 0xFF

            context = ((context << 8) | int(vk)) & ((1 << 64) - 1)
            context_order += 1

    def _feed_feature(self, feature: str, order: int, model_hash: int, value: int) -> None:
        if value == INT_MIN or value < 0:
            return

        order_models = self.models[feature][order]
        order_models.setdefault(model_hash, []).append(int(value))

    def synthesize(self, vks: Sequence[int]) -> Tuple[np.ndarray, np.ndarray]:
        vks_array = np.asarray(vks, dtype=np.uint8)
        hts = self._synthesize_feature("HT", vks_array)
        fts = self._synthesize_feature("FT", vks_array)
        return self._clean_synthesized(hts, fts)

    def _synthesize_feature(self, feature: str, vks: np.ndarray) -> np.ndarray:
        values = np.zeros(len(vks), dtype=np.int64)
        context_order = 1
        context = 0xFF

        for i, vk in enumerate(vks):
            model = self._choose_model(feature, int(vk), context, context_order)
            if model is None:
                values[i] = int(1000 * self.rng.random())
            else:
                values[i] = self._sample_histogram(model)
                if values[i] < 0:
                    values[i] = 0

            context = ((context << 8) | int(vk)) & ((1 << 64) - 1)
            context_order += 1

        return values

    def _choose_model(
        self,
        feature: str,
        vk: int,
        context: int,
        context_order: int,
    ) -> Optional[List[int]]:
        max_order = min(self.max_context_order, context_order)
        for order in range(max_order, -1, -1):
            if order == 0:
                current_context = 0
            else:
                current_context = context & ((1 << (8 * order)) - 1)

            model_hash = int((vk << 56) | current_context)
            observations = self.models[feature][order].get(model_hash)
            if observations is not None and len(observations) >= self.min_observations:
                return observations

        return None

    def _sample_histogram(self, observations: Sequence[int]) -> int:
        sorted_obs = sorted(int(x) for x in observations)
        random_pos = int(self.rng.integers(0, len(sorted_obs)))
        random_offset = float(self.rng.random())
        left = 0.0 if random_pos == 0 else float(sorted_obs[random_pos - 1])
        right = float(sorted_obs[random_pos])
        return int(left + (right - left) * random_offset)

    def _clean_synthesized(self, hts: np.ndarray, fts: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        partition_offsets = [int(i) for i, value in enumerate(fts) if value > SIMPAC_PARTITION_THRESHOLD_MS]

        hts = hts.astype(np.int64, copy=True)
        fts = fts.astype(np.int64, copy=True)
        for i in range(len(fts)):
            if fts[i] < 0:
                fts[i] = np.iinfo(np.int32).max
            if fts[i] > SIMPAC_PARTITION_THRESHOLD_MS:
                fts[i] = SIMPAC_PARTITION_THRESHOLD_MS

            if hts[i] < 0:
                hts[i] = np.iinfo(np.int32).max
            if hts[i] > SIMPAC_PARTITION_THRESHOLD_MS:
                hts[i] = SIMPAC_PARTITION_THRESHOLD_MS

        if len(fts):
            fts[0] = INT_MIN
        for offset in partition_offsets:
            fts[offset] = INT_MIN

        return hts, fts

    def to_state(self) -> Dict[str, Any]:
        serializable_models: Dict[str, List[Dict[str, List[int]]]] = {}
        for feature, feature_models in self.models.items():
            serializable_models[feature] = []
            for order_models in feature_models:
                serializable_models[feature].append({str(k): v for k, v in order_models.items()})

        return {
            "max_context_order": self.max_context_order,
            "min_observations": self.min_observations,
            "models": serializable_models,
        }

    @classmethod
    def from_state(cls, state: Dict[str, Any], random_seed: Optional[int] = None) -> "HistogramProfile":
        profile = cls(
            max_context_order=int(state["max_context_order"]),
            min_observations=int(state["min_observations"]),
            random_seed=random_seed,
        )
        for feature, feature_models in state["models"].items():
            profile.models[feature] = []
            for order_models in feature_models:
                profile.models[feature].append({int(k): [int(x) for x in v] for k, v in order_models.items()})
        return profile


def tensor3_to_tensor5(base_tensor: np.ndarray, profile: HistogramProfile) -> np.ndarray:
    sample = tensor3_to_simpac_sample(base_tensor)
    synth_hts, synth_fts = profile.synthesize(sample.vks)

    synthetic = np.zeros_like(base_tensor, dtype=np.float32)
    synthetic[:, 0] = base_tensor[:, 0]
    synthetic[:, 1] = np.clip(synth_hts / MAX_MILLISECONDS, 0.0, CROP_VALUE)
    synthetic[:, 2] = np.clip(synth_fts / MAX_MILLISECONDS, 0.0, CROP_VALUE)
    synthetic[0, 2] = 0.0

    return np.hstack((base_tensor.astype(np.float32), synthetic[:, 1:].astype(np.float32))).astype(np.float32)


class KeystrokeVerifier:
    def __init__(
        self,
        project_root: Optional[os.PathLike[str] | str] = None,
        baseline_dir: Optional[os.PathLike[str] | str] = None,
        processed_dir: Optional[os.PathLike[str] | str] = None,
        weights_path: Optional[os.PathLike[str] | str] = None,
        t2b_dir: Optional[os.PathLike[str] | str] = None,
        threshold: Optional[float] = None,
        matching_strategy: str = "mean_file",
        random_seed: Optional[int] = 42,
        apply_simpac_cleaning: bool = False,
    ) -> None:
        self.project_root = Path(project_root or Path(__file__).resolve().parent)
        self.baseline_dir = Path(baseline_dir or self.project_root / "baseline_profiles")
        self.processed_dir = Path(processed_dir or self.project_root / "processed_baselines")
        self.weights_path = Path(weights_path or self.project_root / "10persentData_model.weights.h5")
        self.t2b_dir = Path(t2b_dir or self.project_root / "type2branch_model")
        self.threshold = None if threshold is None else float(threshold)
        self.matching_strategy = matching_strategy
        self.random_seed = random_seed
        self.apply_simpac_cleaning = apply_simpac_cleaning

        self.model = None
        self._embedding_cache: Dict[str, np.ndarray] = {}

    def find_baseline_files(self, user_id: str, language: Optional[str] = None) -> List[Path]:
        if not self.baseline_dir.exists():
            return []

        files: List[Path] = []
        for path in self.baseline_dir.glob("keystrokes_*_*.tsv"):
            parsed = parse_baseline_filename(path)
            if parsed is None:
                continue
            if parsed["user_id"] != user_id:
                continue
            if language is not None and parsed["language"] != language:
                continue
            files.append(path)
        return sorted(files, key=lambda p: p.name)

    def resolve_user_id(self, user_id: str, language: Optional[str] = None) -> str:
        """Return the baseline user_id matching this input, ignoring case."""
        requested = str(user_id).strip()
        if not requested:
            return requested
        if self.find_baseline_files(requested, language=language):
            return requested
        if not self.baseline_dir.exists():
            return requested

        matches = set()
        requested_folded = requested.casefold()
        for path in self.baseline_dir.glob("keystrokes_*_*.tsv"):
            parsed = parse_baseline_filename(path)
            if parsed is None:
                continue
            if language is not None and parsed["language"] != language:
                continue
            if parsed["user_id"].casefold() == requested_folded:
                matches.add(parsed["user_id"])

        return sorted(matches)[0] if matches else requested

    def user_exists(self, user_id: str, language: Optional[str] = None) -> bool:
        return bool(self.find_baseline_files(user_id, language=language))

    def cache_path_for_user(self, user_id: str, language: Optional[str] = None) -> Path:
        lang_suffix = language if language else "ALL"
        return self.processed_dir / f"{_safe_cache_stem(user_id)}_{lang_suffix}.npy"

    def ensure_baseline(
        self,
        user_id: str,
        language: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        files = self.find_baseline_files(user_id, language=language)
        if not files:
            raise BaselineNotFoundError(f"No baseline TSV files found for user_id={user_id!r}")

        self.processed_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.cache_path_for_user(user_id, language)
        current_sources = [_source_file_record(path) for path in files]

        if not force and cache_path.exists():
            cached = np.load(cache_path, allow_pickle=True).item()
            if (
                cached.get("cache_version") == CACHE_VERSION
                and cached.get("user_id") == user_id
                and cached.get("language") == language
                and cached.get("apply_simpac_cleaning") == self.apply_simpac_cleaning
                and _records_match(cached.get("source_files", []), current_sources)
            ):
                cached["cache_path"] = str(cache_path.resolve())
                return cached

        cache = self._build_baseline_cache(user_id, language, files, current_sources)
        np.save(cache_path, cache, allow_pickle=True)
        cache["cache_path"] = str(cache_path.resolve())
        return cache

    def _build_baseline_cache(
        self,
        user_id: str,
        language: Optional[str],
        files: Sequence[Path],
        source_records: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        base_tensors: List[np.ndarray] = []
        metadata: List[TemplateMeta] = []

        for path in files:
            parsed = parse_baseline_filename(path) or {}
            sessions = read_tsv_keystrokes(path)
            for section_id, events in sessions.items():
                if not events:
                    continue
                base_tensors.append(events_to_base_tensor(events))
                metadata.append(
                    TemplateMeta(
                        participant_id=events[0].participant_id,
                        source_file=path.name,
                        section_id=section_id,
                        event_count=len(events),
                        language=parsed.get("language"),
                    )
                )

        if not base_tensors:
            raise ValueError(f"Baseline files for user_id={user_id!r} contain no usable keystrokes")

        simpac_samples = [
            tensor3_to_simpac_sample(tensor, apply_cleaning=self.apply_simpac_cleaning)
            for tensor in base_tensors
        ]
        profile = HistogramProfile(random_seed=self.random_seed)
        profile.build(simpac_samples)

        features = np.stack([tensor3_to_tensor5(tensor, profile) for tensor in base_tensors]).astype(np.float32)

        return {
            "cache_version": CACHE_VERSION,
            "created_at": _utc_now_iso(),
            "user_id": user_id,
            "language": language,
            "sequence_length": SEQUENCE_LENGTH,
            "input_features": INPUT_FEATURES,
            "apply_simpac_cleaning": self.apply_simpac_cleaning,
            "source_files": list(source_records),
            "template_meta": [item.to_dict() for item in metadata],
            "features": features,
            "profile_state": profile.to_state(),
        }

    def preprocess_records_for_user(
        self,
        user_id: str,
        records: Sequence[Dict[str, Any]],
        language: Optional[str] = None,
    ) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
        cache = self.ensure_baseline(user_id, language=language)
        profile = HistogramProfile.from_state(cache["profile_state"], random_seed=self.random_seed)
        base_tensors, metadata = self.records_to_base_tensors(records)
        if len(base_tensors) == 0:
            raise ValueError("No usable query keystrokes were supplied")

        query_features = np.stack([tensor3_to_tensor5(tensor, profile) for tensor in base_tensors]).astype(np.float32)
        return query_features, metadata

    def records_to_base_tensors(self, records: Sequence[Dict[str, Any]]) -> Tuple[List[np.ndarray], List[Dict[str, Any]]]:
        grouped: Dict[str, List[RawKeystroke]] = {}
        for row in records:
            try:
                section_id = str(row.get("TEST_SECTION_ID") or row.get("test_section_id") or "query")
                event = RawKeystroke(
                    participant_id=str(row.get("PARTICIPANT_ID") or row.get("participant_id") or ""),
                    test_section_id=section_id,
                    press_time=int(float(row.get("PRESS_TIME", row.get("press_time")))),
                    release_time=int(float(row.get("RELEASE_TIME", row.get("release_time")))),
                    keycode=int(float(row.get("KEYCODE", row.get("keycode")))),
                    letter=str(row.get("LETTER", row.get("letter", ""))),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid query keystroke record: {row}") from exc

            grouped.setdefault(section_id, []).append(event)

        tensors: List[np.ndarray] = []
        metadata: List[Dict[str, Any]] = []
        for section_id, events in grouped.items():
            events.sort(key=lambda e: (e.press_time, e.release_time, e.keycode))
            tensors.append(events_to_base_tensor(events))
            metadata.append(
                {
                    "section_id": section_id,
                    "event_count": len(events),
                    "participant_id": events[0].participant_id if events else "",
                }
            )

        return tensors, metadata

    def preprocess_tsv_for_user(
        self,
        user_id: str,
        tsv_path: os.PathLike[str] | str,
        language: Optional[str] = None,
    ) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
        sessions = read_tsv_keystrokes(Path(tsv_path))
        records: List[Dict[str, Any]] = []
        for events in sessions.values():
            for event in events:
                records.append(
                    {
                        "PARTICIPANT_ID": event.participant_id,
                        "TEST_SECTION_ID": event.test_section_id,
                        "PRESS_TIME": event.press_time,
                        "RELEASE_TIME": event.release_time,
                        "KEYCODE": event.keycode,
                        "LETTER": event.letter,
                    }
                )
        return self.preprocess_records_for_user(user_id, records, language=language)

    def load_model(self) -> Any:
        if self.model is not None:
            return self.model

        if not self.weights_path.exists():
            raise FileNotFoundError(f"Model weights not found: {self.weights_path}")

        model_path = self.t2b_dir / "model.py"
        conf_path = self.t2b_dir / "conf.py"
        if not model_path.exists() or not conf_path.exists():
            raise FileNotFoundError(f"Missing Type2Branch model files under {self.t2b_dir}")

        previous_conf = sys.modules.get("conf")
        try:
            conf_spec = importlib.util.spec_from_file_location("conf", conf_path)
            model_spec = importlib.util.spec_from_file_location("_keystroke_t2b_model", model_path)
            if conf_spec is None or conf_spec.loader is None or model_spec is None or model_spec.loader is None:
                raise ImportError("Unable to create import specs for Type2Branch model files")

            conf_module = importlib.util.module_from_spec(conf_spec)
            sys.modules["conf"] = conf_module
            conf_spec.loader.exec_module(conf_module)

            t2b_model = importlib.util.module_from_spec(model_spec)
            model_spec.loader.exec_module(t2b_model)
        except Exception as exc:
            raise ModelNotLoadedError(
                "Unable to import TensorFlow model code. Activate your kd_web conda "
                "environment and make sure tensorflow is installed."
            ) from exc
        finally:
            if previous_conf is None:
                sys.modules.pop("conf", None)
            else:
                sys.modules["conf"] = previous_conf

        descriptor = {
            "SEQUENCE_LENGTH": SEQUENCE_LENGTH,
            "INPUT_FEATURES": INPUT_FEATURES,
        }
        descriptor = t2b_model.get_model_Type2Branch(descriptor)
        model = descriptor["model"]
        model.load_weights(str(self.weights_path))
        self.model = model
        return self.model

    def embed(self, features: np.ndarray) -> np.ndarray:
        if features.ndim != 3 or features.shape[1:] != (SEQUENCE_LENGTH, INPUT_FEATURES):
            raise ValueError(f"Expected features shaped (N, 100, 5), got {features.shape}")

        model = self.load_model()
        return np.asarray(model.predict(features.astype(np.float32), verbose=0), dtype=np.float32)

    def get_baseline_embeddings(self, cache: Dict[str, Any]) -> np.ndarray:
        cache_path = str(cache.get("cache_path", ""))
        weights_fingerprint = ""
        if self.weights_path.exists():
            stat = self.weights_path.stat()
            weights_fingerprint = f"{self.weights_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"

        key = f"{cache_path}:{weights_fingerprint}"
        if key not in self._embedding_cache:
            self._embedding_cache[key] = self.embed(np.asarray(cache["features"], dtype=np.float32))
        return self._embedding_cache[key]

    def verify_records(
        self,
        user_id: str,
        records: Sequence[Dict[str, Any]],
        language: Optional[str] = None,
        threshold: Optional[float] = None,
        matching_strategy: Optional[str] = None,
    ) -> Dict[str, Any]:
        cache = self.ensure_baseline(user_id, language=language)
        query_features, query_meta = self.preprocess_records_for_user(user_id, records, language=language)

        baseline_embeddings = self.get_baseline_embeddings(cache)
        query_embeddings = self.embed(query_features)

        strategy = matching_strategy or self.matching_strategy
        per_query = []
        scores = []
        for query_embedding, meta in zip(query_embeddings, query_meta):
            score, detail = self.calculate_score(
                baseline_embeddings=baseline_embeddings,
                baseline_meta=cache["template_meta"],
                query_embedding=query_embedding,
                strategy=strategy,
            )
            scores.append(score)
            per_query.append(
                {
                    **meta,
                    "score": score,
                    "detail": detail,
                }
            )

        final_score = float(np.mean(scores))
        active_threshold, threshold_source = self.resolve_threshold(
            baseline_embeddings=baseline_embeddings,
            baseline_meta=cache["template_meta"],
            strategy=strategy,
            override=threshold,
        )
        is_genuine = final_score <= active_threshold

        return {
            "user_id": user_id,
            "is_genuine": bool(is_genuine),
            "score": final_score,
            "threshold": active_threshold,
            "threshold_source": threshold_source,
            "matching_strategy": strategy,
            "query_template_count": int(len(query_embeddings)),
            "baseline_template_count": int(len(baseline_embeddings)),
            "baseline_files": [item["name"] for item in cache["source_files"]],
            "per_query": per_query,
            "model_weights": str(self.weights_path.resolve()),
            "cache_path": cache.get("cache_path"),
            "created_at": _utc_now_iso(),
        }

    def resolve_threshold(
        self,
        baseline_embeddings: np.ndarray,
        baseline_meta: Sequence[Dict[str, Any]],
        strategy: str,
        override: Optional[float] = None,
    ) -> Tuple[float, str]:
        if override is not None:
            return float(override), "override"
        if self.threshold is not None:
            return float(self.threshold), "configured"

        calibrated = self.calibrate_threshold_from_baseline(
            baseline_embeddings=baseline_embeddings,
            baseline_meta=baseline_meta,
            strategy=strategy,
        )
        return calibrated, "baseline_leave_one_out"

    def calibrate_threshold_from_baseline(
        self,
        baseline_embeddings: np.ndarray,
        baseline_meta: Sequence[Dict[str, Any]],
        strategy: str,
    ) -> float:
        if len(baseline_embeddings) < 2:
            return 0.86

        leave_one_out_scores: List[float] = []
        for i in range(len(baseline_embeddings)):
            mask = np.ones(len(baseline_embeddings), dtype=bool)
            mask[i] = False
            score, _ = self.calculate_score(
                baseline_embeddings=baseline_embeddings[mask],
                baseline_meta=[meta for j, meta in enumerate(baseline_meta) if j != i],
                query_embedding=baseline_embeddings[i],
                strategy=strategy,
            )
            leave_one_out_scores.append(score)

        scores = np.asarray(leave_one_out_scores, dtype=np.float32)
        if len(scores) < 5:
            threshold = float(np.max(scores) * 1.10)
        else:
            threshold = float(np.percentile(scores, 95) * 1.10)

        return max(threshold, 1e-6)

    def verify_tsv(
        self,
        user_id: str,
        tsv_path: os.PathLike[str] | str,
        language: Optional[str] = None,
        threshold: Optional[float] = None,
        matching_strategy: Optional[str] = None,
    ) -> Dict[str, Any]:
        sessions = read_tsv_keystrokes(Path(tsv_path))
        records: List[Dict[str, Any]] = []
        for events in sessions.values():
            for event in events:
                records.append(
                    {
                        "PARTICIPANT_ID": event.participant_id,
                        "TEST_SECTION_ID": event.test_section_id,
                        "PRESS_TIME": event.press_time,
                        "RELEASE_TIME": event.release_time,
                        "KEYCODE": event.keycode,
                        "LETTER": event.letter,
                    }
                )
        return self.verify_records(
            user_id=user_id,
            records=records,
            language=language,
            threshold=threshold,
            matching_strategy=matching_strategy,
        )

    def calculate_score(
        self,
        baseline_embeddings: np.ndarray,
        baseline_meta: Sequence[Dict[str, Any]],
        query_embedding: np.ndarray,
        strategy: str,
    ) -> Tuple[float, Dict[str, Any]]:
        distances = np.linalg.norm(baseline_embeddings - query_embedding[None, :], axis=1)

        if strategy == "mean_template":
            return float(np.mean(distances)), {"template_distances": distances.tolist()}

        if strategy == "min_template":
            return float(np.min(distances)), {"template_distances": distances.tolist()}

        if strategy in {"mean_file", "min_file"}:
            grouped: Dict[str, List[float]] = {}
            for distance, meta in zip(distances, baseline_meta):
                grouped.setdefault(str(meta.get("source_file", "unknown")), []).append(float(distance))

            file_scores = {source_file: float(np.mean(values)) for source_file, values in grouped.items()}
            if strategy == "mean_file":
                score = float(np.mean(list(file_scores.values())))
            else:
                score = float(np.min(list(file_scores.values())))
            return score, {"file_scores": file_scores}

        raise ValueError(
            "Unsupported matching_strategy. Use one of: "
            "mean_file, min_file, mean_template, min_template"
        )

    def export_result(self, result: Dict[str, Any], output_path: os.PathLike[str] | str) -> None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


__all__ = [
    "BaselineNotFoundError",
    "HistogramProfile",
    "KeystrokeVerifier",
    "ModelNotLoadedError",
    "events_to_base_tensor",
    "read_tsv_keystrokes",
    "tensor3_to_tensor5",
]


def _discover_users(baseline_dir: Path, language: Optional[str]) -> Dict[str, List[Path]]:
    users: Dict[str, List[Path]] = {}
    if not baseline_dir.exists():
        return users

    for path in sorted(baseline_dir.glob("keystrokes_*_*.tsv")):
        parsed = parse_baseline_filename(path)
        if parsed is None:
            continue
        if language is not None and parsed["language"] != language:
            continue
        users.setdefault(parsed["user_id"], []).append(path)
    return users


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Smoke-test the keystroke verification preprocessing pipeline. "
            "Without --embed, this does not require TensorFlow."
        )
    )
    parser.add_argument("--user", help="Baseline user ID to process. Defaults to all discovered users.")
    parser.add_argument("--language", default="ZH", help="Baseline language filter, e.g. ZH or EN. Use ALL for no filter.")
    parser.add_argument("--force", action="store_true", help="Rebuild processed_baselines cache even if it is current.")
    parser.add_argument("--embed", action="store_true", help="Also load TensorFlow weights and compute embeddings.")
    parser.add_argument("--query-tsv", help="Optional TSV to preprocess or verify against --user.")
    parser.add_argument("--verify", action="store_true", help="With --query-tsv and --embed, run full verification.")
    parser.add_argument("--threshold", type=float, default=None, help="Override verification threshold.")
    parser.add_argument(
        "--strategy",
        default="mean_file",
        choices=["mean_file", "min_file", "mean_template", "min_template"],
        help="Multi-template matching strategy for --verify.",
    )
    args = parser.parse_args(argv)

    language = None if args.language.upper() == "ALL" else args.language.upper()
    verifier = KeystrokeVerifier(matching_strategy=args.strategy)

    print("KeystrokeVerifier smoke test")
    print(f"  project_root:   {verifier.project_root}")
    print(f"  baseline_dir:   {verifier.baseline_dir}")
    print(f"  processed_dir:  {verifier.processed_dir}")
    print(f"  weights_path:   {verifier.weights_path}")
    print(f"  language:       {language or 'ALL'}")

    users = _discover_users(verifier.baseline_dir, language)
    if args.user:
        users = {args.user: verifier.find_baseline_files(args.user, language=language)}

    users = {user_id: files for user_id, files in users.items() if files}
    if not users:
        print("No matching baseline TSV files found.")
        return 1

    print("")
    print("Baselines")
    for user_id, files in users.items():
        print(f"  {user_id}: {len(files)} TSV file(s)")
        cache = verifier.ensure_baseline(user_id, language=language, force=args.force)
        print(
            "    cache: "
            f"{Path(cache['cache_path']).name} "
            f"features={cache['features'].shape} "
            f"templates={len(cache['template_meta'])}"
        )

        if args.embed:
            embeddings = verifier.embed(cache["features"])
            print(f"    embeddings: {embeddings.shape}")

        if args.query_tsv:
            query_features, query_meta = verifier.preprocess_tsv_for_user(user_id, args.query_tsv, language=language)
            print(f"    query: features={query_features.shape} sections={len(query_meta)}")

            if args.verify:
                if not args.embed:
                    print("    verify skipped: add --embed to load the model and run full verification")
                else:
                    result = verifier.verify_tsv(
                        user_id=user_id,
                        tsv_path=args.query_tsv,
                        language=language,
                        threshold=args.threshold,
                        matching_strategy=args.strategy,
                    )
                    print(
                        "    verification: "
                        f"is_genuine={result['is_genuine']} "
                        f"score={result['score']:.6f} "
                        f"threshold={result['threshold']:.6f} "
                        f"source={result['threshold_source']}"
                    )

    print("")
    if not args.embed:
        print("Preprocessing smoke test completed. Add --embed inside kd_web to test TensorFlow weights.")
    else:
        print("Embedding smoke test completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
