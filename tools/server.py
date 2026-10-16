#!/usr/bin/env python3
"""
ronbun ローカルサーバ
- 静的サイト配信（リポジトリ直下をルートに）
- PDFアップロード → 全文＋図を抽出 → Claude の3段ルーティンで解析
    ① 精読: 全文と切り出した図PNGを実際に読み、構造化メモ analysis.md を作る
    ② 生成: メモと図を参照しながら6章の図解ページを作り、index.html にカード追加
    ③ 点検: 6章/図/リンクを機械チェックし、不備があれば Claude が1回修正
- 質問チャット: 解説ページ右下の「💬 質問」→ /api/chat → ページ本文＋論文全文を渡して Claude が回答
- 書き直し: チャットで分からなかった点をもとに、解説ページの該当箇所を Claude が書き直す（/api/revise、バックアップ→点検→失敗なら復元。/api/revert で元に戻す）
- 依存: pymupdf(fitz), claude CLI（既存の認証を利用するのでAPIキー不要）
"""
import os, re, json, time, uuid, shutil, hashlib, difflib, threading, subprocess, urllib.parse, urllib.request
import http.server
try:
    import pymupdf as fitz   # PyMuPDF（新しい名前）
except ImportError:
    import fitz              # 古い PyMuPDF

REPO    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # このファイルの2つ上
PAGES   = os.path.join(REPO, "site", "pages")
FIGSDIR = os.path.join(REPO, "site", "figs")
JOBSDIR = os.path.join(REPO, "tools", "jobs")
CHATDIR = os.path.join(REPO, "tools", "chat")
BACKDIR = os.path.join(REPO, "tools", "backups")   # 書き直し前のページ   # チャット用の作業dir（本文キャッシュ置き場）
EXTRA_PATH = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.claude/local"),
              "/opt/homebrew/bin", "/usr/local/bin"]
CLAUDE  = os.environ.get("CLAUDE_BIN") or shutil.which(
    "claude", path=os.pathsep.join([os.environ.get("PATH", "")] + EXTRA_PATH)) or "claude"
MODEL   = os.environ.get("RONBUN_MODEL", "claude-opus-5-5")
CHAT_MODEL = os.environ.get("RONBUN_CHAT_MODEL", MODEL)
HOST    = os.environ.get("HOST", "127.0.0.1")   # 自分専用: このMacからだけ見える
PORT    = int(os.environ.get("PORT", "8800"))
MAXUP   = 60 * 1024 * 1024   # 60MB
MAXFIGS = 14                 # 切り出す図の上限枚数

JOBS = {}   # job_id -> {state,msg,url,error,title,slug,...}

# ---------- helpers ----------
def slugify(s):
    s = re.sub(r'\.pdf$', '', s, flags=re.I).lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s or ('paper-' + uuid.uuid4().hex[:6])

def unique_slug(base):
    slug, n = base, 2
    while os.path.exists(os.path.join(PAGES, slug + ".html")):
        slug = f"{base}-{n}"; n += 1
    return slug

def extract_text(path):
    d = fitz.open(path)
    txt = "\n".join(d[i].get_text() for i in range(d.page_count))
    n = d.page_count
    d.close()
    return txt, n   # 全ページ・全文（文字数の上限なし）

CAP_RE = re.compile(r'^\s*(?:Figure|Fig\.?|Table|図|表)\s*\.?\s*\d+', re.I)

def extract_figures(pdf_path, slug):
    """キャプション(Figure N / Table N)を手がかりに図領域をPNGで切り出す。
    見つからない論文は埋め込み画像の大きいものを保存するフォールバック。"""
    d = fitz.open(pdf_path)
    outdir = os.path.join(FIGSDIR, slug)
    os.makedirs(outdir, exist_ok=True)
    figs = []

    def save(page, clip, caption, pno):
        clip = clip & page.rect
        if clip.width < 60 or clip.height < 40:
            return
        pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(2, 2))
        name = f"fig-{len(figs)+1:02d}.png"
        pix.save(os.path.join(outdir, name))
        figs.append({
            "file": f"site/figs/{slug}/{name}",
            "rel":  f"../figs/{slug}/{name}",
            "page": pno + 1,
            "caption": " ".join(caption.split())[:400],
        })

    for pno in range(d.page_count):
        if len(figs) >= MAXFIGS:
            break
        page = d[pno]
        blocks = page.get_text("blocks")
        caps = [(fitz.Rect(b[:4]), b[4]) for b in blocks
                if b[6] == 0 and CAP_RE.match(b[4])]
        if not caps:
            continue
        cand = [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
        try:
            cand += page.cluster_drawings()
        except Exception:
            pass
        cand = [r for r in cand if r.width > 40 and r.height > 30]
        for crect, ctext in caps:
            if len(figs) >= MAXFIGS:
                break
            near = [g for g in cand
                    if not (g.x1 < crect.x0 - 20 or g.x0 > crect.x1 + 20)
                    and (-10 <= crect.y0 - g.y1 <= 330 or -10 <= g.y0 - crect.y1 <= 330)]
            above = [g for g in near if g.y1 <= crect.y0 + 10]
            below = [g for g in near if g.y0 >= crect.y1 - 10]
            pick = above if sum(r.get_area() for r in above) >= sum(r.get_area() for r in below) else below
            if not pick:
                continue
            clip = fitz.Rect(pick[0])
            for g in pick[1:]:
                clip |= g
            save(page, clip, ctext, pno)

    if not figs:  # フォールバック: 大きめの埋め込み画像をそのまま
        for pno in range(d.page_count):
            if len(figs) >= MAXFIGS:
                break
            page = d[pno]
            for info in page.get_image_info():
                r = fitz.Rect(info["bbox"])
                if r.width < 150 or r.height < 100:
                    continue
                save(page, r, f"p.{pno+1} の画像", pno)
                if len(figs) >= MAXFIGS:
                    break
    d.close()
    return figs

# ---------- 重複チェック ----------
ARXIV_RE = re.compile(r'arXiv:\s*(\d{4}\.\d{4,5})', re.I)
ARXIV_LINK_RE = re.compile(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})', re.I)
DOI_RE = re.compile(r'\b(10\.\d{4,9}/[^\s"<>]+)', re.I)
HASHES = {}   # PDFの絶対パス -> (mtime, sha256)

def norm_title(t):
    return re.sub(r'[^a-z0-9]+', '', (t or '').lower())

def sha256_file(path):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    if path in HASHES and HASHES[path][0] == mt:
        return HASHES[path][1]
    h = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    HASHES[path] = (mt, h)
    return h

def pdf_identity(data):
    """アップロードされたPDFの指紋: ハッシュ・arXiv ID・DOI・タイトル"""
    ident = {"sha": hashlib.sha256(data).hexdigest(), "arxiv": None, "doi": None, "title": "", "head": ""}
    try:
        d = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return ident
    first = d[0].get_text() if d.page_count else ""
    # 冒頭（Abstract より前＝タイトルと著者のあたり）
    ab = re.search(r'\babstract\b', first, re.I)
    ident["head"] = norm_title(first[:ab.start()] if ab and ab.start() > 40 else first[:800])
    m = ARXIV_RE.search(first)
    ident["arxiv"] = m.group(1) if m else None
    m = DOI_RE.search(first)
    ident["doi"] = m.group(1).rstrip('.').lower() if m else None
    # タイトル: メタデータ → 1ページ目の上半分で一番大きい文字の行
    meta = (d.metadata or {}).get("title") or ""
    title = meta if len(norm_title(meta)) >= 12 and not re.search(r'\.(pdf|docx?|tex|dvi)$|^untitled|^microsoft', meta, re.I) else ""
    if not title and d.page_count:
        page = d[0]; spans = []
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                if abs(l.get("dir", (1, 0))[1]) > 0.1:   # 縦書きのスタンプ（arXiv:…）は除く
                    continue
                for sp in l["spans"]:
                    if sp["text"].strip() and sp["bbox"][1] < page.rect.height * 0.5 and not ARXIV_RE.match(sp["text"].strip()):
                        spans.append((round(sp["size"], 1), sp["text"].strip()))
        if spans:
            big = max(sz for sz, _ in spans)
            title = " ".join(t for sz, t in spans if sz >= big - 0.5)
    ident["title"] = title.strip()[:300]
    d.close()
    return ident

def page_identities():
    """既存の解説ページごとの指紋"""
    out = []
    for fn in sorted(os.listdir(PAGES)):
        if not fn.endswith(".html"):
            continue
        slug = fn[:-5]
        html = open(os.path.join(PAGES, fn), encoding="utf-8", errors="replace").read()
        sub = re.search(r'<p class="sub"[^>]*>(.*?)</p>', html, re.S)
        h1 = re.search(r'<title>(.*?)</title>', html, re.S)
        titles = [re.sub(r'<[^>]+>', '', x.group(1)) for x in (sub, h1) if x]
        titles = [re.split(r'\s+[—–-]\s+', t)[0].strip() for t in titles]
        titles = [re.sub(r'\s*[（(][^）)]*[）)]\s*$', '', t) for t in titles]   # 末尾の（略称）は除く
        shas = []
        for m in re.finditer(r'href="(\.\./\.\./papers/[^"]+\.pdf)"', html, re.I):
            pth = os.path.normpath(os.path.join(PAGES, urllib.parse.unquote(m.group(1))))
            if pth.startswith(os.path.join(REPO, "papers") + os.sep) and os.path.exists(pth):
                shas.append(sha256_file(pth))
        out.append({"slug": slug, "titles": titles, "shas": shas,
                    "arxiv": set(ARXIV_LINK_RE.findall(html)),
                    "doi": set(x.rstrip('.').lower() for x in re.findall(r'doi\.org/(10\.[^"<\s]+)', html, re.I)),
                    "label": titles[0] if titles else slug})
    return out

def find_duplicate(ident):
    nt = norm_title(ident["title"])
    for p in page_identities():
        reason = None
        if ident["sha"] in p["shas"]:
            reason = "同じPDFファイル"
        elif ident["arxiv"] and ident["arxiv"] in p["arxiv"]:
            reason = f"同じ arXiv ID（{ident['arxiv']}）"
        elif ident["doi"] and ident["doi"] in p["doi"]:
            reason = f"同じ DOI（{ident['doi']}）"
        else:
            for t in p["titles"]:
                pt = norm_title(t)
                if len(pt) >= 15 and (pt == nt or difflib.SequenceMatcher(None, pt, nt).ratio() >= 0.9
                                      or (min(len(pt), len(nt)) >= 30 and (pt in nt or nt in pt))
                                      or (len(pt) >= 20 and pt in ident.get("head", ""))):
                    reason = "同じタイトル"; break
        if reason:
            return {"slug": p["slug"], "title": p["label"], "reason": reason,
                    "url": f"/site/pages/{p['slug']}.html"}
    # 生成中のジョブとも比べる
    for J in JOBS.values():
        if J.get("kind") != "revise" and J.get("state") not in ("done", "error") and (
                J.get("sha") == ident["sha"] or (nt and len(nt) >= 15 and J.get("ntitle") == nt)):
            return {"slug": J.get("slug"), "title": J.get("title"), "reason": "いま生成中の論文と同じ", "url": None}
    return None

def parse_multipart(headers, body):
    ctype = headers.get('Content-Type', '')
    if 'boundary=' not in ctype:
        return {}, None, None
    boundary = ctype.split('boundary=', 1)[1].strip().strip('"').encode()
    fields, filename, filedata = {}, None, None
    for part in body.split(b'--' + boundary):
        part = part.strip(b'\r\n')
        if not part or part == b'--' or b'\r\n\r\n' not in part:
            continue
        head, data = part.split(b'\r\n\r\n', 1)
        head_s = head.decode('utf-8', 'replace')
        m_name = re.search(r'name="([^"]*)"', head_s)
        m_file = re.search(r'filename="([^"]*)"', head_s)
        data = data.rstrip(b'\r\n')
        if m_file and m_file.group(1):
            filename, filedata = m_file.group(1), data
        elif m_name:
            fields[m_name.group(1)] = data.decode('utf-8', 'replace')
    return fields, filename, filedata

# ---------- claude routine ----------
def claude_env():
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([os.path.dirname(CLAUDE)] + EXTRA_PATH + [env.get("PATH", "")])
    return env

def claude_run(prompt, timeout, tools=None):
    cmd = [CLAUDE, "-p", prompt, "--model", MODEL, "--permission-mode", "acceptEdits"]
    if tools:
        cmd += ["--tools", tools]
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          timeout=timeout, env=claude_env())

def analysis_prompt(jobrel, npages, nfigs, nlines):
    return f"""あなたは論文の精読担当。作業ディレクトリはカレント（{REPO}）。以下を読み、日本語の構造化メモを {jobrel}/analysis.md に必ず Write せよ。

## 読むもの
1. {jobrel}/fulltext.txt — 論文全文（PDF全{npages}ページ・全{nlines}行。付録も含む）。**Read の offset を変えながら最終行（{nlines}行目）まで必ず全部読む。** 途中で止めない。
2. {jobrel}/figures.json — PDFから切り出した図{nfigs}枚の一覧（file=リポジトリ相対パス、caption=原文キャプション、page=掲載ページ）。
3. **各図のPNGを Read で実際に開いて見る**（最重要。キャプションだけで判断しない）。

## analysis.md の構成（見出しはこの通りに）
# 読了確認
「fulltext.txt を {nlines} 行目まで読了」と1行で書く。
# 書誌情報
タイトル / 著者 / 会議・年 / arXiv ID または DOI（fulltext から読み取れたもののみ。不明なら「不明」）
# ひとことで
TL;DR を2文以内。
# 6章の下書き
## 1. 背景 / ## 2. 問題 / ## 3. 提案 / ## 4. 評価（どんな実験） / ## 5. 結論 / ## 6. 関連研究
各見出しの下に本文の事実に基づく要点を箇条書き。数値・データセット名・比較手法は本文の値をそのまま書く。関連研究は引用元の具体名を挙げる。
# 図の使いどころ
figures.json の各図について1項目ずつ: 実際に見て何が描かれているか / 論文中の Figure/Table 何番に相当するか / 6章のどこで使うと効くか（使わないなら「不使用」と理由）。解説ページで主役級に使える図には★を付ける。
# 用語集
論文の重要な専門用語ごとに: 用語（論文の表記のまま）→ 初出で添える1文の平易な説明。用語を別の言葉やたとえに置き換えない。
# 数式メモ
折りたたみに入れる価値のある式があれば、プレーンテキスト表記で。

捏造禁止: fulltext に無い数値・主張は書かない。最後に analysis.md のパスを一言で報告。"""

def page_prompt(jobrel, slug, topic, pdf_rel):
    topic_line = (f"このトピック名でまとめてほしい: 「{topic}」（既存トピックと一致すればその.topic群に追加、無ければ新しい<div class=\"topic\">として追加）。"
                  if topic else
                  "適切なトピックを analysis.md の内容から判断して、既存の.topic群に追加（合うものが無ければ新しい<div class=\"topic\">として追加）。")
    return f"""あなたは『ronbun』という日本語の論文解説サイト（図解中心の静的サイト）に、新しい論文の図解ページを1枚追加する担当。作業ディレクトリはカレント（{REPO}）。精読メモは既に用意されている。

## まず読む（この順で。必ず全部）
1. {jobrel}/analysis.md — 精読メモ。**これがページの設計図**。書誌情報・6章の下書き・図の使いどころ・用語集をそのまま活かす。
2. {jobrel}/figures.json — 論文から切り出した図PNGの一覧（rel = ページからの相対パス）。
3. preferences.md（説明スタイルの正典＝最優先）
4. site/pages/fourier-features.html（完成テンプレ。ヘッダ/パンくず/hero/フッタ/CSSクラス/インラインSVGの書き方をこの通りに）
5. site/assets/style.css（新クラスは作らず既存クラスのみ）
6. 使う予定の図PNGは Read で開いて内容を確認してから説明を書く。

## 作るページ
- 出力先（必ず Write）: site/pages/{slug}.html
- 相対パス: CSS=`../assets/style.css`、ホーム=`../index.html`、PDF=`{pdf_rel}`
- 原文リンク: analysis.md の arXiv ID / DOI があれば `<a>` で。不明なら省略しPDFリンクのみ。
- hero直後に `<div class="tldr">`（ひとことで言うと）。本文は必ず6章、`<section>`＋`<div class="section-head"><span class="step-num">N</span><h2>…</h2></div>`＋`<p class="lead">…</p>` で連番1〜6: 1.背景 2.問題 3.提案 4.評価（どんな実験） 5.結論 6.関連研究。
- **論文の実図を主役に使う**: 「図の使いどころ」で★が付いた図・採用と書かれた図を、該当章に
  `<figure class="viz from-paper"><img src="（figures.jsonのrel）" alt="…"><figcaption>Figure N: 日本語でこの図の見方を説明（原文キャプションの直訳ではなく、どこに注目すべきか）</figcaption></figure>`
  として埋め込む。実験結果のグラフや手法の全体図は実図が最適。概念の直感説明は自作の手描きインラインSVG（`<figure class="viz">`）。
- 図の数: 実図＋SVG合わせて各章に最低1つ。自作SVGはページ全体で最低3つ、valid なSVG（タグを閉じ属性は引用符）。
- ⚠️ **文体（最重要）: Claude っぽい AI 語を書かない（読点だらけの長文・煽り/キャッチコピー・——の決めゼリフ・「ポイントは/つまり」連発・造語の“飾り”・呼びかけや励まし・絵文字）。人が普通に書くノートの文にする。** CLAUDE.md の文体ルールに従い、書き終えたら見直して直すこと。
- 日本語。**用語はそのまま使う**: 論文の専門用語（GNN・DAG・並列度・強化学習など）は言い換えずにそのまま書く。初めて出たところで1文の説明を添え、以降は同じ用語で統一する。用語の代わりに独自のあだ名・たとえ語を作らない（悪い例:「グラフを読む目」=GNN、「段取り係」=スケジューラ、「人数」=並列度）。たとえ話は補足として1つの考えに1回まで。「たとえば〜のようなもの」と明示し、本文の用語を置き換えない。分かりにくさの原因は用語より説明の飛躍。前提から順に説明する。「ひとことで（TL;DR）」も初出に含む: TL;DR で使う専門用語は2〜3個までに絞り、それぞれ括弧で短く説明する。同じものを英語と日本語で書き分けない（GNN と graph neural network を混ぜない）（用語集を使う）。図が主役で文章は短い補足。パネル並べ禁止(1図=1アイデア)。色の意味づけ（--warm=ざっくり/粗い系、--cool=こまかい/細かい系、--good=良い、--bad=悪い）を一貫させ図注で説明。数式は analysis.md の数式メモから `<details class="deep">` に。外部依存禁止(CDN/MathJax/フォント読込なし)。既存ページと同等の作り込み。
- 数値・主張は analysis.md（＝論文本文由来）の事実だけを使う（捏造厳禁）。
- **理解度チェック（必須）**: 6章の後（原論文リンクの前）に、テンプレ（fourier-features.html）と同じ形の `<section class="quiz">` を置く。
  見出しは `<div class="section-head"><span class="step-num">✓</span><h2>理解度チェック</h2></div>`。
  `<div class="q">` を4問（`<p class="q-text">Q1. …</p>` ＋ `<div class="choices">` に `<button class="choice">` を3つ、正解1つにだけ `data-correct` ＋ `<div class="q-explain">`）。
  丸暗記ではなく「なぜ？」「図のどこを見る？」「これが無いとどうなる？」を問う。誤答も“ありがちな勘違い”にする。正解の位置はばらつかせる。解説は2文以内で「→ N章の〇〇の図」と戻る場所を示す。
- `</body>` の直前に `<script src="../assets/study.js"></script>`（理解度チェックと質問チャットを動かす共通JS）。

## トップページに追加
- site/index.html を Edit。{topic_line}
- 追加するカードは既存と同じ形式: `<a class="paper-card" href="pages/{slug}.html"><span class="tag">解説あり</span><h3>…</h3><p>…</p><div class="row"><span>📄 著者/会議</span><span>🖼️ 図解 6章</span></div></a>`
- index.html 上部のステータスバー、`<section class="newly">`、末尾の `<script>` は絶対に触らない（論文カードを該当 .topic の .cards に1つ足すだけ）。data属性やバッジは付けない（JSが自動付与する）。

## 自己点検（Write前後で確認）
6章が連番で揃う / 理解度チェック4問（各問 data-correct がちょうど1つ）/ study.js 読み込み / 実図の src が figures.json の rel と一致 / SVG3つ以上 / 外部依存なし / 相対パスが上記通り / index.htmlに1枚だけカード追加（statusbar/newly/scriptは無傷）。

最後に、作成したページのパスと採用したトピック名を一言で報告。"""

def regen_prompt(jobrel, slug, pdf_rel, backup_rel):
    """既存ページの作り直し: 新規と同じ手順で書くが、index.html にはカードを足さない"""
    base = page_prompt(jobrel, slug, "", pdf_rel)
    head, rest = base.split("## まず読む", 1)
    body, tail = rest.split("## トップページに追加", 1)
    tail = tail.split("## 自己点検", 1)[1]
    return (head.replace("新しい論文の図解ページを1枚追加する担当", "既存の図解ページを1から作り直す担当")
            + f"""## 作り直しの前提
- 旧ページは {backup_rel} に退避済み。旧ページの文章・たとえ語・見出しは流用しない（読者から「専門用語を避けた変なたとえ語が多くて分かりにくい」と言われて作り直す）。
- 旧ページの 6章「関連研究」にある他の解説ページへのリンク（href="xxx.html"）とパンくずのトピック名だけは Read で確認して引き継ぐ。

## まず読む""" + body + f"""## トップページ
- site/index.html の既存カード（href="pages/{slug}.html"）はそのまま残す。カードを増やさない。
- そのカードの h3 と p の文言だけ、新しいページに合わせて Edit で直してよい（あだ名・たとえ語を使わない）。他は触らない。

## 自己点検""" + tail.replace("index.htmlに1枚だけカード追加", "index.html のカードは1枚のまま"))

def fix_prompt(slug, problems):
    plist = "\n".join(f"- {p}" for p in problems)
    return f"""site/pages/{slug}.html（と site/index.html）の機械チェックで以下の不備が見つかった。作業ディレクトリはカレント（{REPO}）。該当ファイルを Read して不備だけを最小限の Edit で直せ。ページの構成・文章・図は壊さないこと。存在しない画像を参照している場合は site/figs/{slug}/ にある実ファイル名に直すか、その <figure> を自作SVGに置き換える。

不備一覧:
{plist}

直したら修正内容を一言で報告。"""

# ---------- validation ----------
def validate(slug):
    problems = []
    p = os.path.join(PAGES, slug + ".html")
    if not os.path.exists(p):
        return ["ページ site/pages/%s.html が作成されていない" % slug]
    html = open(p, encoding="utf-8", errors="replace").read()
    for n in range(1, 7):
        if not re.search(r'step-num">\s*%d\s*<' % n, html):
            problems.append(f"章{n}の <span class=\"step-num\">{n}</span> が見つからない（6章構成が崩れている）")
    if html.count("<svg") < 3:
        problems.append(f"自作SVGが{html.count('<svg')}個しかない（最低3つ）")
    for m in re.finditer(r'<img[^>]+src="([^"]+)"', html):
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:")):
            problems.append(f"外部/データURI画像を参照している: {src[:80]}")
        elif not os.path.exists(os.path.normpath(os.path.join(PAGES, src))):
            problems.append(f"画像ファイルが存在しない: {src}")
    if re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', html):
        problems.append("外部の script/css を読み込んでいる（オフライン動作が壊れる）")
    qs = re.findall(r'<div class="q">(.*?)<div class="q-explain"', html, re.S)
    if len(qs) < 3:
        problems.append(f"理解度チェック（<section class=\"quiz\"> 内の <div class=\"q\">）が{len(qs)}問しかない（4問ほしい）")
    for i, q in enumerate(qs, 1):
        if q.count("data-correct") != 1:
            problems.append(f"理解度チェック Q{i} の正解（data-correct）が{q.count('data-correct')}個（ちょうど1個にする）")
    if "../assets/study.js" not in html:
        problems.append("</body> 直前の <script src=\"../assets/study.js\"></script> が無い")
    idx = open(os.path.join(REPO, "site", "index.html"), encoding="utf-8", errors="replace").read()
    if f"pages/{slug}.html" not in idx:
        problems.append("site/index.html にこのページのカードが追加されていない")
    return problems

# ---------- job ----------
def run_job(job_id, pdf_rel, slug, topic, npages, figs, regen=False):
    J = JOBS[job_id]
    jobrel = f"tools/jobs/{job_id}"
    ppath = os.path.join(PAGES, slug + ".html")
    backup = None
    if regen:   # 旧ページを退避してから作り直す（失敗したら戻す）
        backup = os.path.join(BACKDIR, f"{slug}.{time.strftime('%Y%m%d%H%M%S')}.html")
        shutil.copy(ppath, backup)
        J["backup"] = os.path.basename(backup).split(".")[-2]
    try:
        _run_job(J, jobrel, pdf_rel, slug, topic, npages, figs, ppath, backup)
    finally:
        if backup and not os.path.exists(ppath):
            shutil.copy(backup, ppath)

def _run_job(J, jobrel, pdf_rel, slug, topic, npages, figs, ppath, backup):
    try:
        # ① 精読（claude CLI は稀に接続が切れるので最大3回試す）
        apath = os.path.join(REPO, jobrel, "analysis.md")
        J.update(state="analyzing",
                 msg=f"① 精読中… 全{npages}ページの本文と図{len(figs)}枚を Claude が読んでいます（数分）")
        r = None
        for attempt in range(3):
            if attempt:
                J.update(msg=f"① 接続が切れたため精読をやり直しています…（{attempt+1}回目）")
                time.sleep(8)
                if os.path.exists(apath):
                    os.remove(apath)
            nlines = sum(1 for _ in open(os.path.join(REPO, jobrel, "fulltext.txt"), encoding="utf-8"))
            r = claude_run(analysis_prompt(jobrel, npages, len(figs), nlines), timeout=1200)
            if os.path.exists(apath):
                break
        if not os.path.exists(apath):
            tail = (r.stderr or r.stdout or "精読メモの生成に失敗しました").strip()
            J.update(state="error", error="①精読で失敗（3回試行）: " + tail[-1200:])
            return

        # ② 生成（同じく最大3回）
        if backup:
            os.remove(ppath)
        J.update(state="generating", msg="② 図を参照しながら6章の図解ページを生成中…（数分）")
        for attempt in range(3):
            if attempt:
                J.update(msg=f"② 接続が切れたため生成をやり直しています…（{attempt+1}回目）")
                time.sleep(8)
            prompt = (regen_prompt(jobrel, slug, pdf_rel, os.path.relpath(backup, REPO)) if backup
                      else page_prompt(jobrel, slug, topic, pdf_rel))
            r = claude_run(prompt, timeout=1200)
            if os.path.exists(ppath):
                break
        if not os.path.exists(ppath):
            tail = (r.stderr or r.stdout or "ページが作られませんでした").strip()
            J.update(state="error", error="②生成で失敗（3回試行）: " + tail[-1200:])
            return

        # ③ 点検 → 必要なら1回だけ修正
        J.update(state="checking", msg="③ 自己点検中…（章構成・図・リンクをチェック）")
        problems = validate(slug)
        if problems:
            J.update(msg=f"③ 点検で不備{len(problems)}件 → Claude が修正中…")
            claude_run(fix_prompt(slug, problems), timeout=600)
            problems = validate(slug)

        note = "" if not problems else f"（注: 未解決の指摘 {len(problems)} 件あり）"
        J.update(state="done", msg="完成しました！" + note,
                 url=f"/site/pages/{slug}.html", warnings=problems)
    except subprocess.TimeoutExpired:
        J.update(state="error", error="タイムアウトしました（生成に時間がかかりすぎ）。もう一度お試しください。")
    except Exception as e:
        J.update(state="error", error=f"{type(e).__name__}: {e}")

# ---------- chat ----------
CHAT_SYSTEM = """あなたは論文解説サイト『ronbun』の家庭教師。読者（=このサイトの持ち主、1人）が解説ページを読みながら質問してくる。
- 日本語で、短く（目安3〜8文）。要点を先に。
- 論文の専門用語はそのまま使い、必要なら1文で説明を添える。独自のあだ名やたとえ語を作らない。たとえ話は役に立つときだけ1回。
- ⚠️ Claude っぽい AI 語を書かない（読点だらけの長文・煽り/キャッチコピー・——の決めゼリフ・「ポイントは/つまり」連発・造語の“飾り”・呼びかけや励まし・絵文字）。人が普通に書くノートの文にする。前置き・褒め言葉・締めのまとめゼリフも不要。
- 直感 → 仕組み → （聞かれたら）数式 の順。数式はプレーンテキスト。
- 「論文に書いてあること」と「一般知識・あなたの解釈」を区別する（後者は「一般には〜」「私の理解では〜」と前置き）。論文に無い数値は作らない。
- 解説ページの章（1.背景〜6.関連研究）や図に触れるときは「3章の〇〇の図」のように場所を示す。
- 理解度チェックの答えを直接聞かれたら、まずヒントを出してから答える。
- 読者が勘違いしていそうなら、やさしく指摘する。
- 読者が「ページを書き直して」「反映して」「ページに入れて」のようにページの修正を頼んだら、どこをどう直すかを1〜2文で答え、回答の最後の行に [[REVISE]] とだけ書く。これでサーバーが書き直しを自動で始める。「ボタンを押して」とは言わない。頼まれていないときは [[REVISE]] を書かない。"""


def page_text(html):
    html = re.sub(r'<(script|style)\b.*?</\1>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<(br|p|div|section|h\d|li|figcaption|figure|details|summary|tr)\b[^>]*>', '\n', html, flags=re.I)
    txt = re.sub(r'<[^>]+>', ' ', html)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&quot;", '"')):
        txt = txt.replace(a, b)
    txt = re.sub(r'[ \t]+', ' ', txt)
    return re.sub(r'\n\s*\n+', '\n', txt).strip()

def paper_text(slug, html):
    """ページ内の PDF リンク（../../papers/...pdf）から論文本文を抜く（キャッシュ付き）"""
    cache = os.path.join(CHATDIR, slug + ".txt")
    if os.path.exists(cache):
        return open(cache, encoding="utf-8").read()
    m = re.search(r'href="(\.\./\.\./papers/[^"]+\.pdf)"', html, re.I)
    pdf = os.path.normpath(os.path.join(PAGES, urllib.parse.unquote(m.group(1)))) if m else ""
    if not pdf or not pdf.startswith(os.path.join(REPO, "papers") + os.sep) or not os.path.exists(pdf):
        # 手元に PDF が無ければ arXiv から取ってくる
        a = re.search(r'href="https://arxiv\.org/(?:pdf|abs)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)', html)
        if not a:
            return ""
        pdf = os.path.join(CHATDIR, slug + ".pdf")
        if not os.path.exists(pdf):
            try:
                req = urllib.request.Request(f"https://arxiv.org/pdf/{a.group(1)}", headers={"User-Agent": "ronbun"})
                data = urllib.request.urlopen(req, timeout=60).read()
            except Exception:
                return ""
            if data[:4] != b"%PDF":
                return ""
            with open(pdf, "wb") as f:
                f.write(data)
    try:
        txt, _ = extract_text(pdf)
    except Exception:
        return ""
    with open(cache, "w", encoding="utf-8") as f:
        f.write(txt)
    return txt

def chat(slug, question, quote, session):
    q = question if not quote else f"（ページ中のこの部分について）「{quote}」\n\n{question}"
    if session:
        prompt = q
    else:
        html = open(os.path.join(PAGES, slug + ".html"), encoding="utf-8", errors="replace").read()
        paper = paper_text(slug, html)
        prompt = (f"# 読者がいま読んでいる解説ページ（site/pages/{slug}.html のテキスト）\n"
                  f"<page>\n{page_text(html)}\n</page>\n\n"
                  + (f"# 論文本文（PDFから抽出）\n<paper>\n{paper}\n</paper>\n\n" if paper else
                     "# 論文本文: 取得できなかった（解説ページと一般知識で答える。その旨を一言添える）\n\n")
                  + f"# 読者の質問\n{q}")
    cmd = [CLAUDE, "-p", "--model", CHAT_MODEL, "--output-format", "json",
           "--tools", "", "--append-system-prompt", CHAT_SYSTEM]
    if session:
        cmd += ["--resume", session]
    r = subprocess.run(cmd, input=prompt, cwd=CHATDIR, capture_output=True, text=True,
                       timeout=300, env=claude_env())
    try:
        out = json.loads(r.stdout)
    except Exception:
        raise RuntimeError((r.stderr or r.stdout or "応答を読めませんでした").strip()[-600:])
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result") or out)[-600:])
    return out.get("result", "").strip(), out.get("session_id")

# ---------- 書き直し ----------
def revise_prompt(slug, transcript, stamp):
    return f"""『ronbun』の解説ページ site/pages/{slug}.html を、読者が質問チャットで分からなかった点をもとに書き直す。作業ディレクトリはカレント（{REPO}）。

## まず読む
1. CLAUDE.md の「最重要の文体ルール」と preferences.md（説明スタイルの正典）
2. site/pages/{slug}.html（全部）
3. 下の質問チャットのやりとり

## やること
- やりとりから「読者がどこで・なぜつまずいたか」を特定する（ページのどの章・どの図・どの言葉か）。
- **つまずいた箇所だけ** を Edit で書き直す。チャットで読者が納得した説明（説明の順序・前提の補足・具体例）をページ側に取り込む。用語は論文の表記のまま使い、あだ名やたとえ語に置き換えない。必要なら短い補足文・図注の改善・小さなインラインSVG（1つ）を足してよい。
- 書き直した塊（段落や figure を含む要素）を `<div class="revised" id="rev-{stamp}">…</div>` で包み、その先頭に `<p class="rev-note">✎ {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} 質問をもとに書き直し</p>` を置く。複数箇所なら2つ目以降の id は rev-{stamp}-2, -3…。
- 6章構成・理解度チェック・study.js の読み込み・既存の図とスクリプトは壊さない。関係ない箇所は触らない。外部依存は足さない。
- ページに無い事実や数値を足さない（チャットの回答で論文本文に基づくと明言されているものは可）。
- 読者の好みとして一般化できる学び（例:「〇〇は図より数値例の方が分かる」）があれば preferences.md の履歴に1行追記してよい。それ以外のファイルは触らない。

## 質問チャットのやりとり
{transcript}

最後に、書き直した箇所を一言で報告。"""

def run_revise(job_id, slug, transcript):
    J = JOBS[job_id]
    page = os.path.join(PAGES, slug + ".html")
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = os.path.join(BACKDIR, f"{slug}.{stamp}.html")
    before = open(page, encoding="utf-8").read()
    with open(backup, "w", encoding="utf-8") as f:
        f.write(before)
    try:
        J.update(state="generating", msg="分からなかった点をもとにページを書き直しています…（1〜3分）")
        claude_run(revise_prompt(slug, transcript, stamp), timeout=900, tools="Read,Edit")
        after = open(page, encoding="utf-8").read()
        if after == before:
            J.update(state="error", error="ページは変更されませんでした。")
            return
        J.update(state="checking", msg="点検中…")
        problems = validate(slug)
        if problems:
            claude_run(fix_prompt(slug, problems), timeout=600, tools="Read,Edit")
            problems = validate(slug)
        if problems:
            with open(page, "w", encoding="utf-8") as f:
                f.write(before)
            J.update(state="error", error="点検で不備が残ったので元に戻しました: " + " / ".join(problems))
            return
        J.update(state="done", msg="書き直しました", url=f"/site/pages/{slug}.html#rev-{stamp}", backup=stamp)
    except subprocess.TimeoutExpired:
        with open(page, "w", encoding="utf-8") as f:
            f.write(before)
        J.update(state="error", error="時間切れでした。ページは元のままです。")
    except Exception as e:
        J.update(state="error", error=f"{type(e).__name__}: {e}")

# ---------- HTTP ----------
class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # 編集した css/js/html がブラウザのキャッシュで古いまま出ないように
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def __init__(self, *a, **k):
        super().__init__(*a, directory=REPO, **k)

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = q.get("id", [""])[0]
            self._json(JOBS.get(jid, {"state": "unknown"}))
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/upload":
            return self.handle_upload()
        if self.path == "/api/chat":
            return self.handle_chat()
        if self.path == "/api/revise":
            return self.handle_revise()
        if self.path == "/api/revert":
            return self.handle_revert()
        if self.path == "/api/regen":
            return self.handle_regen()
        self.send_error(404)

    def handle_upload(self):
        # 安全策: アップロード（=claudeを起動してファイルを書く危険な口）は
        # このMac自身(localhost)からのみ受け付ける。LANの別端末からは拒否。
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return self._json({"error": "アップロードはこのMac本体からのみ可能です（安全のためLAN端末からは不可）"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAXUP:
            return self._json({"error": "ファイルサイズが不正です（最大60MB）"}, 400)
        body = self.rfile.read(length)
        fields, filename, filedata = parse_multipart(self.headers, body)
        if not filedata or not filename:
            return self._json({"error": "ファイルが見つかりません"}, 400)
        if not filename.lower().endswith(".pdf") or filedata[:4] != b"%PDF":
            return self._json({"error": "PDFファイルをドロップしてください"}, 400)

        ident = pdf_identity(filedata)
        if fields.get("force") != "1":
            dup = find_duplicate(ident)
            if dup:
                return self._json({"error": f"この論文はもうあります（{dup['reason']}）: {dup['title']}",
                                   "duplicate": dup}, 409)

        topic = (fields.get("topic") or "").strip()
        topic_folder = slugify(topic) if topic else "uploads"
        os.makedirs(os.path.join(REPO, "papers", topic_folder), exist_ok=True)

        safe_name = re.sub(r'[\\/:*?"<>|]', "_", os.path.basename(filename))
        pdf_path = os.path.join(REPO, "papers", topic_folder, safe_name)
        with open(pdf_path, "wb") as f:
            f.write(filedata)

        try:
            text, npages = extract_text(pdf_path)
        except Exception as e:
            return self._json({"error": f"PDFの本文を読めませんでした: {e}"}, 400)

        slug = unique_slug(slugify(safe_name))
        try:
            figs = extract_figures(pdf_path, slug)
        except Exception:
            figs = []

        jid = uuid.uuid4().hex[:12]
        jobdir = os.path.join(JOBSDIR, jid)
        os.makedirs(jobdir, exist_ok=True)
        with open(os.path.join(jobdir, "fulltext.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(jobdir, "figures.json"), "w", encoding="utf-8") as f:
            json.dump(figs, f, ensure_ascii=False, indent=1)

        pdf_rel = f"../../papers/{topic_folder}/{safe_name}"
        JOBS[jid] = {"state": "queued",
                     "msg": f"受け取りました（全{npages}ページ・図{len(figs)}枚を抽出）。精読を開始します…",
                     "title": safe_name, "slug": slug, "pages": npages, "figs": len(figs),
                     "sha": ident["sha"], "ntitle": norm_title(ident["title"])}
        threading.Thread(target=run_job, args=(jid, pdf_rel, slug, topic, npages, figs),
                         daemon=True).start()
        self._json({"job_id": jid, "slug": slug, "pages": npages, "figs": len(figs)})

    def _local_json(self):
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self._json({"error": "このMac本体からのみ使えます"}, 403); return None
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"error": "リクエストが不正です"}, 400); return None
        slug = str(req.get("slug", ""))
        if not re.fullmatch(r'[A-Za-z0-9._-]+', slug) or not os.path.exists(os.path.join(PAGES, slug + ".html")):
            self._json({"error": "このページは解説ページとして見つかりません"}, 404); return None
        return req, slug

    def handle_revise(self):
        got = self._local_json()
        if not got:
            return
        req, slug = got
        msgs = req.get("msgs") or []
        lines = []
        for m in msgs:
            if isinstance(m, dict) and m.get("role") in ("me", "ai"):
                lines.append(("読者: " if m["role"] == "me" else "回答: ") + str(m.get("text", "")))
        if not any(l.startswith("読者: ") for l in lines):
            return self._json({"error": "質問がまだありません"}, 400)
        if any(J.get("slug") == slug and J.get("kind") == "revise" and J.get("state") not in ("done", "error")
               for J in JOBS.values()):
            return self._json({"error": "このページは書き直し中です"}, 409)
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"state": "queued", "kind": "revise", "slug": slug, "msg": "受け付けました"}
        threading.Thread(target=run_revise, args=(jid, slug, "\n\n".join(lines)), daemon=True).start()
        self._json({"job_id": jid})

    def handle_regen(self):
        """既存ページを PDF から作り直す（旧ページは tools/backups/ に退避、/api/revert で戻せる）"""
        got = self._local_json()
        if not got:
            return
        req, slug = got
        if any(J.get("slug") == slug and J.get("state") not in ("done", "error") for J in JOBS.values()):
            return self._json({"error": "このページは作業中です"}, 409)
        html = open(os.path.join(PAGES, slug + ".html"), encoding="utf-8", errors="replace").read()
        m = re.search(r'href="(\.\./\.\./papers/[^"]+\.pdf)"', html, re.I)
        pdf_path = os.path.normpath(os.path.join(PAGES, urllib.parse.unquote(m.group(1)))) if m else ""
        if not pdf_path or not pdf_path.startswith(os.path.join(REPO, "papers") + os.sep) or not os.path.exists(pdf_path):
            return self._json({"error": "このページの PDF が papers/ に見つかりません"}, 404)
        text, npages = extract_text(pdf_path)
        try:
            figs = extract_figures(pdf_path, slug)
        except Exception:
            figs = []
        jid = uuid.uuid4().hex[:12]
        jobdir = os.path.join(JOBSDIR, jid)
        os.makedirs(jobdir, exist_ok=True)
        with open(os.path.join(jobdir, "fulltext.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(jobdir, "figures.json"), "w", encoding="utf-8") as f:
            json.dump(figs, f, ensure_ascii=False, indent=1)
        JOBS[jid] = {"state": "queued", "kind": "regen", "slug": slug, "pages": npages, "figs": len(figs),
                     "msg": f"作り直しを開始します（全{npages}ページ・図{len(figs)}枚）"}
        threading.Thread(target=run_job, args=(jid, m.group(1), slug, "", npages, figs, True), daemon=True).start()
        self._json({"job_id": jid})

    def handle_revert(self):
        got = self._local_json()
        if not got:
            return
        req, slug = got
        stamp = str(req.get("backup", ""))
        if not re.fullmatch(r'\d{14}', stamp):
            return self._json({"error": "戻す先が不正です"}, 400)
        src = os.path.join(BACKDIR, f"{slug}.{stamp}.html")
        if not os.path.exists(src):
            return self._json({"error": "バックアップが見つかりません"}, 404)
        with open(src, encoding="utf-8") as f, open(os.path.join(PAGES, slug + ".html"), "w", encoding="utf-8") as g:
            g.write(f.read())
        self._json({"ok": True})

    def handle_chat(self):
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return self._json({"error": "チャットはこのMac本体からのみ使えます"}, 403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
        except Exception:
            return self._json({"error": "リクエストが不正です"}, 400)
        slug = str(req.get("slug", ""))
        question = str(req.get("question", "")).strip()[:4000]
        quote = str(req.get("quote") or "").strip()[:800]
        session = req.get("session")
        if not re.fullmatch(r'[A-Za-z0-9._-]+', slug) or not os.path.exists(os.path.join(PAGES, slug + ".html")):
            return self._json({"error": "このページは解説ページとして見つかりません"}, 404)
        if session is not None and not re.fullmatch(r'[0-9a-f-]{36}', str(session)):
            session = None
        if not question:
            return self._json({"error": "質問が空です"}, 400)
        try:
            answer, sid = chat(slug, question, quote, session)
        except subprocess.TimeoutExpired:
            return self._json({"error": "時間切れでした（5分）。質問を短くして送り直してください。"})
        except Exception as e:
            if session:   # 古いセッションが消えていたら、新しい会話としてやり直す
                try:
                    answer, sid = chat(slug, question, quote, None)
                    revise = "[[REVISE]]" in answer
                    return self._json({"answer": answer.replace("[[REVISE]]", "").strip(),
                                       "session": sid, "restarted": True, "revise": revise, "cli_dir": CHATDIR})
                except Exception as e2:
                    e = e2
            return self._json({"error": f"回答に失敗しました: {e}"})
        revise = "[[REVISE]]" in answer
        answer = answer.replace("[[REVISE]]", "").strip()
        self._json({"answer": answer, "session": sid, "revise": revise, "cli_dir": CHATDIR})

    def log_message(self, fmt, *a):
        # API へのアクセスだけ記録（失敗の原因を後から追えるように）
        if "/api/" in (getattr(self, "path", "") or ""):
            with open(os.path.join(REPO, "tools", "server.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + self.address_string() + " " + (fmt % a) + "\n")

def main():
    os.makedirs(JOBSDIR, exist_ok=True)
    os.makedirs(CHATDIR, exist_ok=True)
    os.makedirs(BACKDIR, exist_ok=True)
    httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    if not os.path.exists(CLAUDE) and not shutil.which(CLAUDE):
        print("⚠ claude コマンドが見つかりません。Claude Code を入れてログインするか CLAUDE_BIN を指定してください。")
    print(f"ronbun: http://{HOST}:{PORT}/site/  （追加: /site/upload.html）")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
