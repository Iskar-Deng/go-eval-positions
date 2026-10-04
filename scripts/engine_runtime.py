"""Paths and provenance shared by local Metal and Linux CUDA runs."""
import hashlib
import os
from pathlib import Path
import platform
from find_atari import PROJECT

MODEL_NAME='kata1-b18c384nbt-s9996604416-d4316597426.bin.gz'
MODEL_SHA256='9d7a6afed8ff5b74894727e156f04f0cd36060a24824892008fbb6e0cba51f1d'


def model_path():
    return Path(os.environ.get('KATAGO_MODEL',PROJECT/'.local/katago/models'/MODEL_NAME)).resolve()


def config_path():
    default='katago-analysis.cfg'
    return Path(os.environ.get('KATAGO_ANALYSIS_CONFIG',PROJECT/'configs'/default)).resolve()


def provenance():
    model=model_path();config=config_path()
    binary=Path(os.environ.get('KATAGO_BIN',PROJECT/'.local/katago/bin/katago')).resolve()
    return dict(model=str(model),model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
                engine_binary=str(binary),engine_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                engine_config=str(config),engine_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                platform=platform.platform())
