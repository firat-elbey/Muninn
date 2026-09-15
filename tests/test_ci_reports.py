"""Keep hosted test failures visible without exposing complete test output."""

import importlib.util
from pathlib import Path


def reporter():
    path = Path(__file__).resolve().parents[1] / "tools" / "report_test_failures.py"
    spec = importlib.util.spec_from_file_location("report_test_failures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_annotations_include_failures_but_not_captured_output(tmp_path, capsys):
    path = tmp_path / "results.xml"
    path.write_text('<testsuite><testcase classname="suite" name="case">'
                    '<failure message="Expected 1, got 2." />'
                    '<system-out>Do not publish captured output.</system-out>'
                    '</testcase><testcase name="passed" /></testsuite>')
    assert reporter().report(path) == 1
    assert capsys.readouterr().out == "::error::suite.case: Expected 1, got 2.\n"


def test_annotation_data_cannot_inject_workflow_commands(tmp_path, capsys):
    path = tmp_path / "results.xml"
    path.write_text('<testsuite><testcase name="case&#10;::warning::forged">'
                    '<error message="first%&#13;&#10;::error::forged" />'
                    '</testcase></testsuite>')
    assert reporter().report(path) == 1
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert "case%0A::warning::forged" in output
    assert "first%25%0D%0A::error::forged" in output


def test_missing_or_invalid_report_does_not_mask_test_step(tmp_path, capsys):
    path = tmp_path / "results.xml"
    assert reporter().report(path) == 0
    path.write_text("not XML")
    assert reporter().report(path) == 0
    assert capsys.readouterr().out.count("::warning::") == 2


def test_annotation_count_and_message_size_are_bounded(tmp_path, capsys):
    path = tmp_path / "results.xml"
    path.write_text("<testsuite>" + "".join(
        f'<testcase name="case{index}"><failure message="{"x" * 2000}" /></testcase>'
        for index in range(60)) + "</testsuite>")
    assert reporter().report(path) == 60
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 51
    assert sum(line.startswith("::error::") for line in lines) == 50
    assert all(len(line) <= 1050 for line in lines)
    assert "10 additional" in lines[-1]
