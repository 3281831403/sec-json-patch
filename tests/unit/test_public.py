from patch import apply_patch, patch_batch, resolve


def test_replace_single_value():
    doc = {"config": {"a/b": 1, "a~1b": 2}}
    result = apply_patch(
        doc,
        [{"op": "replace", "path": "/config/a~1b", "value": 9}],
    )
    assert result["config"]["a/b"] == 9
    assert result["config"]["a~1b"] == 2


def test_add_single_value():
    doc = {"config": {"a/b": {"x": 1}, "a~1b": {"x": 2}}}
    result = apply_patch(
        doc,
        [{"op": "add", "path": "/config/a~1b/y", "value": 3}],
    )
    assert result["config"]["a/b"] == {"x": 1, "y": 3}
    assert result["config"]["a~1b"] == {"x": 2}


def test_remove_single_value():
    doc = {"config": {"a/b": {"x": 1}, "a~1b": {"x": 2}}}
    result = apply_patch(
        doc,
        [{"op": "remove", "path": "/config/a~1b"}],
    )
    assert result == {"config": {"a~1b": {"x": 2}}}


def test_resolve_nested_key():
    doc = {"service": {"a/b": {"enabled": True}, "a~1b": {"enabled": False}}}
    assert resolve(doc, "/service/a~1b/enabled") is True


def test_batch_keeps_documents_independent():
    docs = [{"service": {"a": 1}}, {"service": {"b": 2}}]
    patches = [
        [{"op": "add", "path": "/service/x", "value": 10}],
        [{"op": "add", "path": "/service/y", "value": 20}],
    ]
    output = patch_batch(docs, patches)
    assert output["results"][0] == {"service": {"a": 1, "x": 10}}
    assert output["results"][1] == {"service": {"b": 2, "y": 20}}