# -*- coding: utf-8 -*-
"""
地震自動通知エンジン
- 気象庁系API(P2PQuake)から地震情報を取得
- 震度5弱以上の観測市区町村を抽出
- 施設マスタと照合し、対象施設あり/なしを判定
- 通知本文(メールHTML / Teamsテキスト)を生成
- 重複通知防止(state.json)
- 通知先Webhook(Power Automate)へHTTP POST

設定は環境変数で行う（GitHub Actions Secrets 等）:
  WEBHOOK_URL      : Power Automate(Teams Workflows)のHTTP POST先
  MAIL_TO          : 通知メール宛先(カンマ区切り)
  MIN_SCALE        : 通知する最小震度コード(既定45=震度5弱)
"""
import json, os, sys, urllib.request, urllib.error, datetime, argparse, html, traceback

BASE = os.path.dirname(os.path.abspath(__file__))
FACILITIES_PATH = os.path.join(BASE, "facilities.json")
STATE_PATH = os.path.join(BASE, "state.json")
# limit: 群発地震時に取りこぼさないよう余裕を持たせる
API_URL = "https://api.p2pquake.net/v2/history?codes=551&limit=50"

# 震度コード -> 表示名
SCALE_NAME = {10:"震度1",20:"震度2",30:"震度3",40:"震度4",
              45:"震度5弱",46:"震度5強",50:"震度6弱",55:"震度6強",60:"震度7"}

def scale_label(s):
    return SCALE_NAME.get(s, f"震度コード{s}")

# ---------- データ取得 ----------
def fetch_quakes(url=API_URL):
    req = urllib.request.Request(url, headers={"User-Agent": "jishin-notify/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

# ---------- 施設マスタ ----------
def load_facilities(path=FACILITIES_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

# ---------- 状態(重複防止) ----------
def load_state(path=STATE_PATH):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"notified": {}}

def save_state(state, path=STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

# ---------- 照合 ----------
def bare_pref(pref):
    """都道府県名から末尾の都/道/府/県を除いた略称(例: 茨城県->茨城, 東京都->東京)"""
    for suf in ("都","道","府","県"):
        if pref.endswith(suf):
            return pref[:-1]
    return pref

def strong_points(quake, min_scale=45):
    """震度min_scale以上の観測点を返す [{pref, addr, scale}]"""
    out = []
    for p in quake.get("points", []):
        if isinstance(p.get("scale"), int) and p["scale"] >= min_scale:
            out.append({"pref": p.get("pref",""), "addr": p.get("addr",""), "scale": p["scale"]})
    return out

def match_facilities(facilities, points):
    """
    観測点(pref,addr,scale)群と施設を照合。
    施設は「同一県」かつ「match_tokensのいずれかが観測点addrの部分文字列」で一致とみなす。
    戻り値: [{facility, scale(最大), matched_addr}]
    """
    results = []
    for f in facilities:
        f_pref = f["pref"]
        f_pref_bare = bare_pref(f_pref)
        best_scale = -1
        best_addr = None
        for p in points:
            # 県一致(気象庁は略称のこともあるため両対応)
            if not (p["pref"] == f_pref or p["pref"] == f_pref_bare or bare_pref(p["pref"]) == f_pref_bare):
                continue
            for tok in f["match_tokens"]:
                if tok and tok in p["addr"]:
                    if p["scale"] > best_scale:
                        best_scale = p["scale"]
                        best_addr = p["addr"]
                    break
        if best_scale >= 0:
            results.append({"facility": f, "scale": best_scale, "matched_addr": best_addr})
    return results

# ---------- 通知本文 ----------
def build_payload(quake, points, matched, min_scale=45):
    eq = quake.get("earthquake", {})
    hypo = eq.get("hypocenter", {}) or {}
    occurred = html.escape(str(eq.get("time", "")))
    max_scale = eq.get("maxScale", 0)
    hypo_name = html.escape(str(hypo.get("name", "不明")))
    mag = html.escape(str(hypo.get("magnitude", "")))
    has_target = len(matched) > 0

    subject = ("【地震通知・対象施設あり】" if has_target else "【地震通知・対象施設なし】") \
              + f"{scale_label(max_scale)} {hypo_name}（{occurred}）"

    # Teams用テキスト(HTML描画のため改行は<br>)
    lines = []
    if has_target:
        lines.append("■ 震度5弱以上の地震が発生しました。【対象施設があります】")
    else:
        lines.append("■ 震度5弱以上の地震が発生しましたが、【対象施設はありませんでした】")
    lines.append(f"震源: {hypo_name} ／ 最大{scale_label(max_scale)} ／ M{mag}")
    lines.append(f"発生時刻: {occurred}")
    lines.append("")
    if has_target:
        lines.append(f"対象施設: {len(matched)}件")
        for m in sorted(matched, key=lambda x:-x["scale"]):
            fa = m["facility"]
            lines.append(f"・{html.escape(fa['name'])}（{scale_label(m['scale'])}）")
            lines.append(f"　　所在地: {html.escape(fa['pref'])}{html.escape(fa['address'])}")
            lines.append(f"　　市町村: {html.escape(fa['municipality'])} ／ TEL {html.escape(fa['tel'])}")
    else:
        lines.append("登録施設一覧との照合結果、震度5弱以上に該当する施設はありませんでした。")
    teams_text = "<br>".join(lines)

    # メール用HTML
    rows = ""
    for m in sorted(matched, key=lambda x:-x["scale"]):
        fa = m["facility"]
        rows += (f"<tr><td style='padding:6px;border:1px solid #ccc'>{html.escape(fa['name'])}</td>"
                 f"<td style='padding:6px;border:1px solid #ccc;color:#c00;font-weight:bold'>{scale_label(m['scale'])}</td>"
                 f"<td style='padding:6px;border:1px solid #ccc'>{html.escape(fa['municipality'])}</td>"
                 f"<td style='padding:6px;border:1px solid #ccc'>{html.escape(fa['pref'])}{html.escape(fa['address'])}</td>"
                 f"<td style='padding:6px;border:1px solid #ccc'>{html.escape(fa['tel'])}</td></tr>")
    banner_color = "#c0392b" if has_target else "#2c3e50"
    banner_text = "対象施設あり" if has_target else "対象施設なし"
    if has_target:
        body_html = (f"<p>震度5弱以上の地震が発生しました。<b>対象施設があります（{len(matched)}件）。</b>"
                     f"施設名・所在地・市町村・震度・発生時刻をご確認ください。</p>"
                     f"<table style='border-collapse:collapse;font-size:14px'>"
                     f"<tr style='background:#f2f2f2'>"
                     f"<th style='padding:6px;border:1px solid #ccc'>施設名</th>"
                     f"<th style='padding:6px;border:1px solid #ccc'>震度</th>"
                     f"<th style='padding:6px;border:1px solid #ccc'>市町村</th>"
                     f"<th style='padding:6px;border:1px solid #ccc'>所在地</th>"
                     f"<th style='padding:6px;border:1px solid #ccc'>電話</th></tr>{rows}</table>")
    else:
        body_html = ("<p>震度5弱以上の地震が発生しましたが、登録施設一覧との照合の結果、"
                     "<b>対象施設はありませんでした。</b></p>")
    mail_html = (
        f"<div style='font-family:sans-serif'>"
        f"<div style='background:{banner_color};color:#fff;padding:10px 14px;font-size:16px;font-weight:bold'>"
        f"地震通知 ／ {banner_text}</div>"
        f"<div style='padding:12px 14px'>"
        f"<p style='margin:4px 0'><b>震源:</b> {hypo_name}　<b>最大震度:</b> {scale_label(max_scale)}　<b>規模:</b> M{mag}</p>"
        f"<p style='margin:4px 0'><b>発生時刻:</b> {occurred}</p>"
        f"{body_html}"
        f"<p style='color:#888;font-size:12px;margin-top:14px'>本メールは地震自動通知システムにより送信されています。"
        f"（震度5弱以上を検知した際に自動送信）</p>"
        f"</div></div>")

    return {
        "hasTarget": has_target,
        "targetCount": len(matched),
        "subject": subject,
        "teamsText": teams_text,
        "mailHtml": mail_html,
        "hypocenter": hypo_name,
        "maxScale": scale_label(max_scale),
        "occurredAt": occurred,
        "quakeId": quake.get("id",""),
    }

# ---------- Webhook送信 ----------
def normalize_mail_to(mail_to):
    """宛先の区切りをOutlook(V2)が確実に解釈できるセミコロン区切りに正規化。"""
    s = (mail_to or "").replace("、", ",").replace("；", ";").replace("，", ",")
    parts = []
    for chunk in s.replace(",", ";").replace("\n", ";").split(";"):
        a = chunk.strip()
        if a:
            parts.append(a)
    return ";".join(parts)

def post_webhook(payload, url, mail_to):
    body = dict(payload)
    body["mailTo"] = normalize_mail_to(mail_to)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "ignore")

def notify_failure(webhook, mail_to, message):
    """システム障害(API取得失敗・通知処理失敗)をWebhook経由でベストエフォート通知する。
    通知自体の送信失敗は握りつぶす(呼び出し元のループを止めないため)。"""
    if not webhook:
        return
    try:
        payload = {
            "hasTarget": False, "targetCount": 0,
            "subject": "【地震通知システム・エラー】" + message[:80],
            "teamsText": "⚠ 地震通知システムでエラーが発生しました。<br>" + html.escape(message),
            "mailHtml": (
                "<div style='font-family:sans-serif'>"
                "<div style='background:#c0392b;color:#fff;padding:10px 14px;font-size:16px;font-weight:bold'>"
                "地震通知システム・エラー通知</div>"
                f"<div style='padding:12px 14px'><p>{html.escape(message)}</p>"
                "<p style='color:#888;font-size:12px'>GitHub Actionsの実行ログを確認してください。</p>"
                "</div></div>"),
            "hypocenter": "", "maxScale": "", "occurredAt": "", "quakeId": "",
        }
        post_webhook(payload, webhook, mail_to)
    except Exception:
        pass

# ---------- メイン ----------
JST = datetime.timezone(datetime.timedelta(hours=9))

def quake_age_minutes(eq):
    t = eq.get("time", "")
    try:
        dt = datetime.datetime.strptime(t, "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST)
        return (datetime.datetime.now(JST) - dt).total_seconds() / 60.0
    except Exception:
        return 0.0

def run(dry_run=False):
    min_scale = int(os.environ.get("MIN_SCALE", "45"))
    max_age = float(os.environ.get("MAX_AGE_MIN", "60"))
    webhook = os.environ.get("WEBHOOK_URL", "")
    mail_to = os.environ.get("MAIL_TO", "")
    facilities = load_facilities()
    state = load_state()
    # ハートビート: 1日1回state.jsonを変化させ、リポジトリを常にアクティブに保つ
    # (GitHubは60日間リポジトリ更新がないとスケジュール実行を無効化するため)
    state["heartbeat"] = datetime.datetime.now(JST).date().isoformat()
    try:
        quakes = fetch_quakes()
    except Exception as e:
        print(f"地震情報の取得に失敗しました: {e}", file=sys.stderr)
        traceback.print_exc()
        notify_failure(webhook, mail_to, f"地震情報APIの取得に失敗しました: {e}")
        save_state(state)
        return []
    handled = []
    for q in quakes:
        qid = q.get("id", "")
        try:
            eq = q.get("earthquake", {})
            if not isinstance(eq.get("maxScale"), int) or eq["maxScale"] < min_scale:
                continue
            if qid in state["notified"]:
                continue
            if quake_age_minutes(eq) > max_age:
                state["notified"][qid] = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "skipped": "old"}
                continue
            points = strong_points(q, min_scale)
            matched = match_facilities(facilities, points)
            payload = build_payload(q, points, matched, min_scale)
            if dry_run or not webhook:
                print("=== DRY-RUN ==="); print(payload["subject"]); print(payload["teamsText"])
            else:
                status, resp = post_webhook(payload, webhook, mail_to)
                print(f"通知送信 status={status} quake={qid}")
            state["notified"][qid] = {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                "hasTarget": payload["hasTarget"], "targetCount": payload["targetCount"]}
            handled.append(qid)
        except Exception as e:
            # 1件の失敗で他の地震の通知がブロックされないよう、ここで打ち切らず次に進む。
            # notifiedに記録しないため、次回実行時にこの地震は再試行される。
            print(f"地震ID={qid} の処理中にエラーが発生しました: {e}", file=sys.stderr)
            traceback.print_exc()
            notify_failure(webhook, mail_to, f"地震通知処理でエラーが発生しました(id={qid}): {e}")
            continue
    if len(state["notified"]) > 200:
        items = sorted(state["notified"].items(), key=lambda kv: kv[1].get("at",""))
        state["notified"] = dict(items[-200:])
    save_state(state)
    if not handled:
        print("新規の震度5弱以上の地震はありません。")
    return handled

def send_test_notification():
    webhook = os.environ.get("WEBHOOK_URL", "")
    mail_to = os.environ.get("MAIL_TO", "")
    if not webhook:
        print("WEBHOOK_URL が未設定です。"); return
    facilities = load_facilities()
    now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    q1 = {"id": "TEST-HIT", "earthquake": {"time": now, "maxScale": 50, "hypocenter": {"name": "【テスト】茨城県沖", "magnitude": 6.4}}}
    pts1 = [{"pref": "茨城県", "addr": "水戸市中央", "scale": 50}]
    p1 = build_payload(q1, pts1, match_facilities(facilities, pts1))
    p1["subject"] = "【テスト送信】" + p1["subject"]
    s1, _ = post_webhook(p1, webhook, mail_to)
    print(f"テスト通知(対象あり) 送信 status={s1} 対象{p1['targetCount']}件")
    q2 = {"id": "TEST-NONE", "earthquake": {"time": now, "maxScale": 45, "hypocenter": {"name": "【テスト】山梨県東部・富士五湖", "magnitude": 5.5}}}
    pts2 = [{"pref": "山梨県", "addr": "富士河口湖町長浜", "scale": 45}]
    p2 = build_payload(q2, pts2, match_facilities(facilities, pts2))
    p2["subject"] = "【テスト送信】" + p2["subject"]
    s2, _ = post_webhook(p2, webhook, mail_to)
    print(f"テスト通知(対象なし) 送信 status={s2}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        send_test_notification()
    else:
        run(dry_run=args.dry_run)
