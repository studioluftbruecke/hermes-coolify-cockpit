"""Behaviour: text from outside (a pasted build log, a notebook entry) reaches the model with the
common app secrets masked: the plugin's own rules first, then Hermes' redactor as a second pass.
"""

import importlib
import json

R = "Xk29fQ7LmP3vT8wZ1bN6"  # filler for synthetic secrets, built at run time
def test_common_app_secrets_in_a_pasted_log_are_masked(plugin):
    diag = importlib.import_module(plugin.__name__ + ".diagnose")
    secrets = {
        "Laravel APP_KEY": (f"RUN echo APP_KEY=base64:{R}{R}= >> .env", f"base64:{R}{R}"),
        "RAILS_MASTER_KEY": (f"--build-arg RAILS_MASTER_KEY={R}{R}", f"{R}{R}"),
        "JWT_KEY": (f"JWT_KEY={R}11", f"{R}11"),
        "LICENSE_KEY": (f"LICENSE_KEY={R}12", f"{R}12"),
    }
    log = "\n".join(["#1 [internal] load build definition"] + [line for line, _ in secrets.values()]
                    + ["error: exit code 1"])
    text = json.dumps(diag.render_for_model(diag.classify(log, source="pasted")))
    leaked = [name for name, (_, secret) in secrets.items() if secret in text]
    assert leaked == []


def test_a_notebook_entry_masks_an_app_key(plugin, tmp_path):
    notes = importlib.import_module(plugin.__name__ + ".notes")
    book = notes.Notebook(tmp_path)
    book.append(f"the shop needs APP_KEY=base64:{R}{R}= after a restore", author="agent")
    assert f"{R}{R}" not in book.read(10_000)["text"]


def test_hermes_redactor_runs_as_a_second_pass(plugin, monkeypatch):
    import sys
    import types

    redact = importlib.import_module(plugin.__name__ + ".redact")
    fake = types.ModuleType("agent.redact")
    setattr(fake, "redact_sensitive_text", lambda text, force=False: text.replace("CANARY-" + R, "***"))
    if "agent" not in sys.modules:
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.redact", fake)
    text, count = redact.redact(f"line one\nshape only Hermes knows: CANARY-{R}\nline three")
    assert f"CANARY-{R}" not in text and count == 1
    assert text.count("\n") == 2, "line numbers still point at the original log"
