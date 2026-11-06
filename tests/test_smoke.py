from pr_review import hello


def test_smoke() -> None:
    assert hello() == "Hello from pr-review!"
