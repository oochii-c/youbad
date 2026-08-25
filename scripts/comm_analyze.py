#!/usr/bin/env python3
"""Communication analyzer for Claude Code session logs.

Parses .jsonl session transcripts and reports how the user and Claude
communicate: volume, message length, question/imperative/redirect signals,
friction points (scope-overrun redirects, redo/error), and token efficiency.

Usage:
    python comm_analyze.py [PROJECT_DIR]

PROJECT_DIR defaults to the current Claude Code project log dir. Pass a
path to a ~/.claude/projects/<slug> folder, or "ALL" to sweep every project.
"""
import json, os, glob, re, sys, io
from collections import Counter, defaultdict
from datetime import datetime

# force utf-8 stdout (Windows terminals mangle Korean otherwise)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")


def resolve_dirs(arg):
    if arg and arg.upper() == "ALL":
        return sorted(d for d in glob.glob(os.path.join(PROJECTS, "*")) if os.path.isdir(d))
    if arg:
        return [arg]
    # default: current working dir's slug (cwd path with sep -> -)
    cwd = os.getcwd()
    slug = re.sub(r"[:\\/]+", "-", cwd)
    cand = os.path.join(PROJECTS, slug)
    if os.path.isdir(cand):
        return [cand]
    # fallback: biggest project dir
    dirs = [d for d in glob.glob(os.path.join(PROJECTS, "*")) if os.path.isdir(d)]
    return [max(dirs, key=lambda d: sum(os.path.getsize(f) for f in glob.glob(d + "/*.jsonl")))] if dirs else []


def load(dirs):
    user_msgs, asst_ct = [], 0
    tok = [0, 0, 0, 0]  # in, out, cache_read, cache_create
    times = []
    sessions = set()
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            sessions.add(os.path.basename(f)[:8])
            for line in open(f, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                m = o.get("message", {})
                if isinstance(m, dict):
                    u = m.get("usage")
                    if u:
                        tok[0] += u.get("input_tokens", 0)
                        tok[1] += u.get("output_tokens", 0)
                        tok[2] += u.get("cache_read_input_tokens", 0)
                        tok[3] += u.get("cache_creation_input_tokens", 0)
                    if m.get("role") == "assistant":
                        c = m.get("content")
                        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip() for b in c):
                            asst_ct += 1
                ts = o.get("timestamp")
                if ts:
                    try:
                        times.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
                    except Exception:
                        pass
                if o.get("type") != "user" or o.get("isMeta"):
                    continue
                if not isinstance(m, dict) or m.get("role") != "user":
                    continue
                c = m.get("content")
                txt = None
                if isinstance(c, str):
                    txt = c
                elif isinstance(c, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        continue
                    txt = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                if not txt:
                    continue
                txt = txt.strip()
                if not txt or txt.startswith("<") or txt.startswith("[Request") or txt.startswith("[Image"):
                    continue
                if "system-reminder" in txt[:40] or txt.startswith("Caveman"):
                    continue
                user_msgs.append(txt)
    return user_msgs, asst_ct, tok, times, sessions


# --- Axis 1: speech intent (mutually exclusive, single label per message) ---
RE_REDIRECT = re.compile(r"^(아니|아냐)([ ,.]|$)")
RE_WH       = re.compile(r"뭐|뭔|무엇|무슨|어디|어떻게|어케|어케됨|어케함|언제|얼마|몇|어느|어떤")
RE_WHY      = re.compile(r"왜(?!냐)")            # 왜 but not 왜냐(하면) = explanation, not question
RE_REQUEST  = re.compile(r"해줘|해봐|해라|하자|해줄|해주|해봐|고쳐|만들|바꿔|추가|지워|삭제해|넣어|"
                         r"올려|띄워|돌려|커밋|머지|리셋|롤백|추가해|해도|하고 |줄래|줄수|해볼래|해볼")
RE_PROBE    = re.compile(r"아니[니냐]|아니야\??$|아닌가|거 ?아니|것 ?아니|잖")  # tag confirmation
RE_QMARK    = re.compile(r"[?？]")
RE_QEND     = re.compile(r"(니|냐|나|까|가|지|래|죠|어|해|음|엄)\s*[?？]?\s*$")


def is_question(t):
    return bool(RE_QMARK.search(t)) or bool(re.search(r"(니|냐|나|까|ㄹ까|을까|는가|은가|나요|까요|을래|ㄹ래|어\?|음\?)\s*[?？]?\s*$", t))


def classify_intent(t):
    """Single label. Priority: redirect > info-question(WH/why) > imperative > probe > question > statement."""
    if RE_REDIRECT.search(t):
        return "redirect"
    q = is_question(t)
    if q and RE_WHY.search(t):
        return "why"
    if q and RE_WH.search(t):
        return "question"          # info question with WH word beats a stray command verb
    if RE_REQUEST.search(t) and not (q and RE_WH.search(t)):
        return "imperative"
    if q and RE_PROBE.search(t):
        return "probe"
    if q:
        return "question"
    return "statement"


INTENTS = ["redirect", "imperative", "question", "probe", "why", "statement"]

# --- Axis 2: friction (independent flags) --- Axis 3: affect (independent flags) ---
SIGNALS = {
    "redo 다시":        r"다시",
    "wrong 틀림":       r"틀렸|틀린|잘못|틀려",
    "broken 안됨":      r"안돼|안됨|안 돼|안된|안 된|안되",
    "error 에러":       r"에러|오류|[Ee]rror",
    "praise 칭찬":      r"고마|감사|굿|[Gg]ood|훌륭|완벽|좋아|좋다|좋네|나이스|[Nn]ice",
    "frustration 짜증": r"ㅅㅂ|시발|짜증|답답|아씨|에휴|아 진짜|아진짜",
}


def report(dirs, user_msgs, asst_ct, tok, times, sessions):
    n = len(user_msgs)
    if n == 0:
        print("no user messages found in:", dirs)
        return
    print("=" * 60)
    print("COMMUNICATION ANALYSIS")
    print("scope:", ", ".join(os.path.basename(d) for d in dirs))
    print("=" * 60)
    if times:
        span = (max(times) - min(times)).days
        print(f"period: {min(times).date()} ~ {max(times).date()} ({span}d)  sessions: {len(sessions)}")
    print(f"user msgs: {n}   assistant text msgs: {asst_ct}   ratio: {asst_ct/n:.1f}:1")

    lens = sorted(len(t) for t in user_msgs)
    kor = sum(1 for t in user_msgs if re.search(r"[가-힣]", t))
    print(f"\nuser msg length: median={lens[n//2]} mean={sum(lens)/n:.0f} p90={lens[int(n*0.9)]} max={max(lens)} chars")
    print(f"Korean: {kor/n*100:.0f}%   <=30 chars: {sum(1 for l in lens if l<=30)/n*100:.0f}%")

    ti, to, cr, cc = tok
    print(f"\ntokens: fresh_in={ti:,} out={to:,} cache_read={cr:,} cache_create={cc:,}")
    if cr + ti:
        print(f"cache hit ratio: {cr/(cr+ti)*100:.1f}%")

    print("\n--- intent (single label, mutually exclusive) ---")
    labels = [classify_intent(t) for t in user_msgs]
    ic = Counter(labels)
    for name in INTENTS:
        print(f"  {name:12} {ic[name]:4} ({ic[name]/n*100:4.1f}%)")

    print("\n--- friction / affect (independent flags) ---")
    for name, pat in SIGNALS.items():
        c = sum(1 for t in user_msgs if re.search(pat, t))
        print(f"  {name:18} {c:4} ({c/n*100:4.1f}%)")

    print("\n--- friction sample (redirects) ---")
    shown = 0
    for t in user_msgs:
        if re.match(r"^(아니|아냐)([ ,.]|$)", t):
            print("  •", t[:90])
            shown += 1
            if shown >= 12:
                break

    print("\n--- interpretation ---")
    redir = sum(1 for t in user_msgs if re.match(r"^(아니|아냐)([ ,.]|$)", t))
    praise = sum(1 for t in user_msgs if re.search(SIGNALS["praise 칭찬"], t))
    frust = sum(1 for t in user_msgs if re.search(SIGNALS["frustration 짜증"], t))
    q = sum(1 for t in user_msgs if "?" in t)
    print(f"  terse: median {lens[n//2]} chars, {sum(1 for l in lens if l<=30)/n*100:.0f}% under 30")
    print(f"  Socratic: {q/n*100:.0f}% questions, {redir} sentence-start redirects")
    print(f"  low affect: praise {praise}, frustration {frust} across {n} msgs")
    print("  => primary friction = scope overrun (redirects), not bugs")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    dirs = resolve_dirs(arg)
    report(dirs, *load(dirs))
