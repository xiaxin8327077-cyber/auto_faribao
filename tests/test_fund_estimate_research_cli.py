import json
from pathlib import Path
import subprocess
import sys


def test_offline_cli_imports_public_data_and_writes_honest_insufficient_history(tmp_path):
    source = tmp_path/'public.json'
    source.write_text(json.dumps({'identities': [{'product_id': 'p', 'code': '918273', 'share_class': 'A'}],
                                  'navs': [], 'profiles': [], 'original_final_predictions': []}), encoding='utf-8')
    output = tmp_path/'report.json'
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/fund_estimate_research.py'),
                             '--db', str(tmp_path/'research.db'), '--import-json', str(source),
                             '--output', str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['items'][0]['history_count'] == 0
    assert report['items'][0]['point_in_time']['selected_strategy'] is None
    assert report['items'][0]['promotion_allowed'] is False
    assert output.with_suffix('.md').exists()
