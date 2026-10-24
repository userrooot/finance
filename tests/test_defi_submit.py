"""Submission is impossible by construction: the stub always raises and no signing code exists in arb/defi."""
import re
from pathlib import Path

import pytest

from arb.defi import submit as submit_mod
from arb.defi.submit import SubmissionDisabled, submit

DEFI_DIR = Path(__file__).resolve().parent.parent / "arb" / "defi"
FORBIDDEN = ("eth_account", "sign_transaction", "private_key", "eth_sendRawTransaction")


def test_submit_always_raises():
    with pytest.raises(SubmissionDisabled, match="Simulation only"):
        submit()
    with pytest.raises(SubmissionDisabled):
        submit("0xroute", chain="arbitrum", flag=True)
    assert issubclass(SubmissionDisabled, RuntimeError)


def test_no_flag_or_environment_switch_in_submit():
    src = Path(submit_mod.__file__).read_text()
    assert "environ" not in src and "getenv" not in src and "import os" not in src
    assert len(re.findall(r"\bdef \w+", src)) == 1  # only `submit`


def test_no_signing_or_sending_code_in_arb_defi():
    files = sorted(DEFI_DIR.glob("*.py"))
    assert files, "arb/defi has no sources?"
    for f in files:
        text = f.read_text().lower()
        for bad in FORBIDDEN:
            assert bad.lower() not in text, f"{f.name} mentions {bad}"


def test_only_read_rpc_methods_are_used():
    methods = set()
    for f in DEFI_DIR.glob("*.py"):
        methods |= set(re.findall(r'"(eth_[a-z]+[A-Z]\w*|eth_call)"', f.read_text()))
    assert methods <= {"eth_call", "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getCode",
                       "eth_getBalance", "eth_getStorageAt"}, methods
