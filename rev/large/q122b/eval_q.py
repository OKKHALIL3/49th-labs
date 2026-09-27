"""Usage: python eval_q.py base|tuned  -> writes eval_<which>.json in this dir (never touches rev/data)."""
import sys, time, json, traceback
from common import ru, OUT, summ, write_json, finalize
from rev import tasks
from rev import eval as ev
which = sys.argv[1]
rows = tasks.load("test")
t0 = time.time()
try:
    ck = ru.load_checkpoint_ref(OUT / "checkpoint.json") if which == "tuned" else None
    res = ev.run_eval(which, rows, workers=8, checkpoint=ck)
    out = {"which": which, "summary": summ(res), "elapsed_s": round(time.time() - t0, 1),
           "checkpoint": json.loads((OUT / "checkpoint.json").read_text()) if which == "tuned" else None,
           "details": res}
except Exception as e:
    traceback.print_exc()
    out = {"which": which, "error": f"{type(e).__name__}: {e}", "summary": None}
write_json(OUT / f"eval_{which}.json", out)
print(json.dumps(out.get("summary")), out.get("error"), f"elapsed {time.time()-t0:.1f}s", flush=True)
finalize()
