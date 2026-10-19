from caselens_evals.__main__ import main


def test_eval_stub_passes_without_gold_set() -> None:
    assert main() == 0
