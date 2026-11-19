import json
from schedulehandoff import equipment_checkout, equipment_return


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_checkout_and_return_scans(tmp_path):
    db = tmp_path / 'ledger.db'
    loan = {'version': 'v1', 'rows': [{'id': 'K1', 'borrower': 'T1',
            'serials': ['CAM1', 'MIC1']}]}
    assert equipment_checkout.checkout(db, write(tmp_path, 'loan.json', loan))[0]['status'] == '已借出'
    returned = {'version': 'v1', 'rows': [{'id': 'R1', 'loan': 'K1', 'serials': ['CAM1']}]}
    assert equipment_return.receive(db, write(tmp_path, 'return.json', returned))[0]['status'] == '缺件'
