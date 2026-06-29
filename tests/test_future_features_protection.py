import json


def test_load_config_respects_disabled_future_features(tmp_path, monkeypatch):
    import ghost

    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps({"model": "x", "enable_future_features": False}),
        encoding="utf-8",
    )

    monkeypatch.setattr(ghost, "CONFIG_FILE", cfg_path)

    loaded = ghost.load_config()

    # The owner can disable future features from the config file/dashboard.
    assert loaded["enable_future_features"] is False


def test_load_config_defaults_future_features_true(tmp_path, monkeypatch):
    import ghost

    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"model": "x"}), encoding="utf-8")

    monkeypatch.setattr(ghost, "CONFIG_FILE", cfg_path)

    loaded = ghost.load_config()

    assert loaded["enable_future_features"] is True


def test_config_patch_rejects_disabling_enable_future_features():
    from ghost_config_tool import build_config_tools

    config_patch = next(t for t in build_config_tools() if t["name"] == "config_patch")

    raw = config_patch["execute"]({"enable_future_features": False})
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "cannot be false" in payload["error"]


def test_dashboard_config_put_allows_disabling_enable_future_features(tmp_path, monkeypatch):
    import ghost
    import ghost_dashboard

    cfg_path = tmp_path / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"model": "x"}), encoding="utf-8")
    monkeypatch.setattr(ghost, "CONFIG_FILE", cfg_path)

    app = ghost_dashboard.create_app()
    client = app.test_client()

    resp = client.put(
        "/api/config",
        json={"enable_future_features": False},
    )

    # The owner is allowed to disable autonomous feature implementation.
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["config"]["enable_future_features"] is False
    # And the choice persists across reloads.
    assert ghost.load_config()["enable_future_features"] is False
