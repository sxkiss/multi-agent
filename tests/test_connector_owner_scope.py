"""重-1 回归：连接器 owner 隔离默认拒绝。"""
from connector_hub import connectors_service as svc


def test_owner_scope_matching():
    admin_inst = {"instance_id": "a", "owner": "admin"}
    card_inst = {"instance_id": "b", "owner": "ABC123"}
    legacy_inst = {"instance_id": "c", "owner": ""}

    assert svc._owner_matches(admin_inst, "*") is True
    assert svc._owner_matches(card_inst, "*") is True
    assert svc._owner_matches(card_inst, "ABC123") is True
    assert svc._owner_matches(card_inst, "OTHER") is False
    assert svc._owner_matches(card_inst, "") is False
    assert svc._owner_matches(legacy_inst, "") is False
    # None 仅用于受信内部调用
    assert svc._owner_matches(legacy_inst, None) is True


def test_delete_wrong_owner_is_noop(monkeypatch):
    instances = [{"instance_id": "x", "owner": "alice"}]
    saved = []
    monkeypatch.setattr(svc, "_instances", lambda: list(instances))
    monkeypatch.setattr(svc, "_save_instances", lambda rows: saved.append(rows))
    assert svc.delete_instance("x", owner="bob") is False
    assert saved == []
    assert len(instances) == 1
