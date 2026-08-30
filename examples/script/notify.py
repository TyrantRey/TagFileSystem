# Code by AkinoAlice@TyrantRey

"""Demo add-on: problems reach you only through handlers like these.

``@action.err()`` receives every P1 *and* P0 (level and above); ``.warn()``
would also get P2. Replace the ``print`` with whatever your NAS offers —
a webhook, an e-mail, a push notification — ``ctx.retry`` lets the handler
retry a failed run. Problems this handler raises itself are logged, never
re-dispatched, so it cannot loop.
"""

from tag_file_system import action


@action.err()
def notify(problem, ctx):
    line = f"[{problem.severity.value}] {problem.kind}: {problem.message}"
    print(line)
    (ctx.root / ".tfs" / "PROBLEMS.log").open("a", encoding="utf-8").write(line + "\n")


@action.info()
def journal(problem, ctx):
    """Everything, including ``run.ok``: the ordinary feed."""
    if problem.kind == "run.ok":
        ctx.log(problem.message)
