"""既存の解説ページから知見メモ（knowledge/papers/<slug>.md）をまとめて作る。
使い方: python3 tools/build_knowledge.py [slug ...]   （省略時はメモがまだ無いページ全部）"""
import os, sys, json, glob
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S

def job_for(slug):
    """生成時のジョブ（全文と精読メモ）を探す。新しいもの優先"""
    for d in sorted(glob.glob(os.path.join(S.JOBSDIR, "*")), key=os.path.getmtime, reverse=True):
        try:
            figs = json.load(open(os.path.join(d, "figures.json")))
        except Exception:
            continue
        if any(f"/figs/{slug}/" in (x.get("rel", "") + x.get("file", "")) for x in figs):
            a = os.path.join(d, "analysis.md")
            return os.path.join(d, "fulltext.txt"), a if os.path.exists(a) else ""
    return "", ""

def one(slug):
    txt, ana = job_for(slug)
    err = S.make_knowledge(slug, txt, ana, push=False)
    print(f"{'NG' if err else 'ok'} {slug} {err}", flush=True)

if __name__ == "__main__":
    slugs = sys.argv[1:] or sorted(f[:-5] for f in os.listdir(S.PAGES) if f.endswith(".html")
                                   and not os.path.exists(os.path.join(S.KDIR, "papers", f[:-5] + ".md")))
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, slugs))
    with S.KLOCK:
        S.write_kindex()
        S.kgit("add", "-A"); S.kgit("commit", "-qm", f"Add {len(slugs)} notes"); S.kgit("pull", "-q", "--rebase"); print(S.kgit("push", "-q").stderr)
