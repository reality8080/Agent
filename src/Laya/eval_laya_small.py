"""Small labeled eval for Laya triage (Vi/En/Ja). Shadow mode only."""
from __future__ import annotations
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from Laya.laya_adapter import LayaTriageAdapter, DEFAULT_TRIAGE_QUESTIONS  # chỉnh import nếu cần

# --- 18 cases (6 EN / 6 VI / 6 JA). Label = expected task_type ---
# risk label chỉ dùng để quan sát, chưa tính accuracy chính
CASES = [
    # English
    {"id": "en01", "lang": "en", "text": "Please implement a small patch for the login form.", "task_type": "coding", "risk": "medium"},
    {"id": "en02", "lang": "en", "text": "Research the official docs of the payment API and summarize the auth flow.", "task_type": "research", "risk": "low"},
    {"id": "en03", "lang": "en", "text": "Write an implementation plan for adding 2FA before any code change.", "task_type": "planning", "risk": "low"},
    {"id": "en04", "lang": "en", "text": "Review this pull request diff and list security issues only. Do not edit code.", "task_type": "review", "risk": "low"},
    {"id": "en05", "lang": "en", "text": "Deploy the staging build to production and run smoke tests.", "task_type": "coding", "risk": "high"},
    {"id": "en06", "lang": "en", "text": "What is the weather in Tokyo today?", "task_type": "other", "risk": "low"},
    # Vietnamese
    {"id": "vi01", "lang": "vi", "text": "Tôi cần bạn nghiên cứu tài liệu API và viết plan trước khi code.", "task_type": "research", "risk": "low"},
    {"id": "vi02", "lang": "vi", "text": "Sửa lỗi null pointer trong hàm tính tổng đơn hàng.", "task_type": "coding", "risk": "medium"},
    {"id": "vi03", "lang": "vi", "text": "Lập kế hoạch chi tiết module thanh toán, liệt kê file sẽ đụng tới.", "task_type": "planning", "risk": "low"},
    {"id": "vi04", "lang": "vi", "text": "Chỉ review diff, không được sửa source. Báo cáo findings theo severity.", "task_type": "review", "risk": "low"},
    {"id": "vi05", "lang": "vi", "text": "Push code lên main và deploy production ngay.", "task_type": "coding", "risk": "high"},
    {"id": "vi06", "lang": "vi", "text": "Hôm nay trời Hà Nội thế nào?", "task_type": "other", "risk": "low"},
    # Japanese
    {"id": "ja01", "lang": "ja", "text": "このバグを修正する前に、影響範囲を調査してください。", "task_type": "research", "risk": "low"},  # hoặc coding tùy cách hiểu; label research vì "調査"
    {"id": "ja02", "lang": "ja", "text": "ログイン画面の小さなパッチを実装してください。", "task_type": "coding", "risk": "medium"},
    {"id": "ja03", "lang": "ja", "text": "二要素認証を追加するための実装計画を書いてください。コードはまだ書かないでください。", "task_type": "planning", "risk": "low"},
    {"id": "ja04", "lang": "ja", "text": "このPRの差分だけをレビューし、セキュリティ問題を報告してください。コードは変更しないでください。", "task_type": "review", "risk": "low"},
    {"id": "ja05", "lang": "ja", "text": "本番環境にデプロイしてスモークテストを実行してください。", "task_type": "coding", "risk": "high"},
    {"id": "ja06", "lang": "ja", "text": "今日の東京の天気は？", "task_type": "other", "risk": "low"},
]


def main():
    adapter = LayaTriageAdapter(device="cpu", model="multilingual", confidence_threshold=0.55)
    rows = []
    correct = 0
    by_lang = defaultdict(lambda: {"n": 0, "ok": 0, "conf": [], "lat": [], "abstain": 0})

    print("Running eval (first case may still be slow if model was unloaded)...\n")
    for c in CASES:
        t0 = time.perf_counter()
        d = adapter.triage(c["text"])
        lat = (time.perf_counter() - t0) * 1000
        ok = d.task_type == c["task_type"]
        if ok:
            correct += 1
        bl = by_lang[c["lang"]]
        bl["n"] += 1
        bl["ok"] += int(ok)
        bl["conf"].append(d.confidence)
        bl["lat"].append(d.latency_ms)
        bl["abstain"] += int(d.abstain)

        row = {
            "id": c["id"],
            "lang": c["lang"],
            "gold_task": c["task_type"],
            "pred_task": d.task_type,
            "gold_risk": c["risk"],
            "pred_risk": d.risk,
            "correct": ok,
            "confidence": round(d.confidence, 3),
            "abstain": d.abstain,
            "latency_ms": round(d.latency_ms, 1),
            "model": d.model_used,
        }
        rows.append(row)
        mark = "OK" if ok else "XX"
        print(f"{mark} {c['id']} gold={c['task_type']:8} pred={d.task_type:8} conf={d.confidence:.3f} abs={d.abstain} {d.latency_ms:.0f}ms")

    n = len(CASES)
    acc = correct / n
    print("\n=== Summary ===")
    print(f"Accuracy task_type: {correct}/{n} = {acc:.3f}")
    for lang, s in by_lang.items():
        a = s["ok"] / s["n"] if s["n"] else 0
        mean_c = sum(s["conf"]) / len(s["conf"]) if s["conf"] else 0
        mean_l = sum(s["lat"]) / len(s["lat"]) if s["lat"] else 0
        print(f"  {lang}: acc={a:.3f}  mean_conf={mean_c:.3f}  abstain={s['abstain']}/{s['n']}  mean_lat={mean_l:.0f}ms")

    out = Path("laya_eval_results.json")
    out.write_text(json.dumps({"accuracy": acc, "n": n, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    # Simple confusion
    conf_mat = Counter((r["gold_task"], r["pred_task"]) for r in rows)
    print("\nConfusion (gold → pred):")
    for (g, p), cnt in sorted(conf_mat.items()):
        print(f"  {g:8} → {p:8} : {cnt}")


if __name__ == "__main__":
    main()