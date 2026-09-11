"""Verify evaluation failure sensitivity and real Windows process-tree cleanup separately."""

import ctypes
import os
import subprocess
import sys

import pytest

from infra.ocr.supervise import stop_tree
from scripts.evaluate_scanned_fixtures import score_output, score_table


def test_financial_evaluator_rejects_correct_numbers_in_wrong_columns() -> None:
    """Swapping reporting years must reduce accuracy even when all numbers appear somewhere."""
    expected = [["FY2024", "FY2025"], ["168000.00", "180000.00"]]
    blocks = [
        {
            "block_label": "table",
            "block_content": (
                "<table><tr><td>FY2024</td><td>FY2025</td></tr>"
                "<tr><td>180000.00</td><td>168000.00</td></tr></table>"
            ),
        }
    ]
    result = score_table(blocks, expected)
    assert result["exact_shape"]
    assert result["exact_cells"] == 2
    assert result["expected_cells"] == 4
    assert result["exact_numeric_cells"] == 0


def test_missing_duplicate_and_spanned_tables_do_not_receive_full_credit() -> None:
    """Ambiguous grids cannot silently select the first convenient matching table."""
    block = {"block_label": "table", "block_content": "<tr><td colspan='2'>a</td></tr>"}
    for blocks in ([], [block], [block, block]):
        assert score_table(blocks, [["a"]])["exact_cells"] == 0


def test_column_evaluator_detects_reversed_order_and_blank_hallucination() -> None:
    """Word recovery does not establish reading order or appropriate blank-page abstention."""
    raw = b'{"parsing_res_list":[{"block_label":"text","block_content":"Right Left"}]}'
    result = score_output(raw, {"columns": [["Left"], ["Right"]], "must_fail": "empty"})
    assert result["column_lines_found"] == 2
    assert not result["column_reading_order_correct"]
    assert not result["blank_correctly_empty"]


@pytest.mark.skipif(os.name != "nt", reason="The experiment supervisor is Windows-specific")
def test_timeout_cleanup_terminates_owned_child_and_grandchild() -> None:
    """Exercise actual launcher-style descendants rather than assuming Popen.kill handles them."""
    command = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print(p.pid,flush=True); time.sleep(30)"
    )
    parent = subprocess.Popen(  # noqa: S603 - fixed local test code, no user input or shell.
        [sys.executable, "-c", command],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = None
    try:
        assert parent.stdout is not None
        descendant = int(parent.stdout.readline())
        handle = kernel.OpenProcess(0x00100000, False, descendant)
        assert handle
        stop_tree(parent)
        assert parent.poll() is not None
        assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        if parent.poll() is None:
            stop_tree(parent)
        if handle:
            kernel.CloseHandle(handle)
        if parent.stdout:
            parent.stdout.close()
