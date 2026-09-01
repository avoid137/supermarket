"""confirm 端点对未解决 review（含空候选「未识别品类」）的拦截回归测试。

背景：未识别商品存在两处反馈缺口——
1. 空检测（0 个目标但 mode 仍成功）只显示淡淡「共 0 个目标」，无醒目告警；
2. 未解决 review（含「识别到商品但无法确定品类」空候选项）在支付时会被
   to_bill_lines 静默跳过，confirm 原来只查 if not snapshot.detections 就放行，
   导致未识别项凭空消失、账单缺漏。

本测试验证：confirm 在存在未解决 review 时强制拦截（400，
提示「还有 N 件未确认（其中 M 件未识别到品类）」），且不破坏正常
（全 auto）结账流程，也不会误拦已人工指认（resolved）的项。

运行：D:/envs/supermarketenv/python.exe tests/test_confirm_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app
from app.services.store import session_store
from app.models.schemas import Candidate, Detection
from app.repositories import product_repo

PASSED = 0
FAILED = 0


def ok(label: str):
    global PASSED
    PASSED += 1
    print(f"  [PASS] {label}")


def fail(label: str, detail: str):
    global FAILED
    FAILED += 1
    print(f"  [FAIL] {label} -- {detail}")


client = TestClient(app)


def _new_session() -> str:
    r = client.post("/api/v1/checkout/sessions")
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _first_sku():
    products = product_repo.list_products()
    assert products, "商品库为空"
    return products[0].sku_id, products[0].name


def test_empty_detections_blocked():
    """空检测 → 400「尚未识别任何商品」（原行为，回归守护）。"""
    sid = _new_session()
    r = client.post(f"/api/v1/checkout/sessions/{sid}/confirm")
    detail = r.json().get("detail") or ""
    if r.status_code == 400 and "尚未识别" in detail:
        ok("空检测 → 400 尚未识别任何商品")
    else:
        fail("空检测拦截", f"status={r.status_code} detail={detail}")


def test_unresolved_empty_candidate_review_blocked():
    """未解决空候选 review（未识别品类）→ 400「未确认（未识别到品类）」。"""
    sid = _new_session()
    snap = session_store.get(sid)
    snap.detections = [
        Detection(
            box_id="V1", x=10, y=30, w=18, h=20,
            candidates=[], quantity=1, state="review",
            message="识别到商品但无法确定品类，已转入人工复核",
        )
    ]
    session_store.save(snap)
    r = client.post(f"/api/v1/checkout/sessions/{sid}/confirm")
    detail = r.json().get("detail") or ""
    if r.status_code == 400 and "未确认" in detail and "未识别" in detail:
        ok("未解决空候选 review → 400 未确认（未识别到品类）")
    else:
        fail("未解决空候选 review 拦截", f"status={r.status_code} detail={detail}")


def test_unresolved_with_candidates_review_blocked():
    """未解决「有候选但低置信」review → 400「未确认」（空候选之外也要拦）。"""
    sid = _new_session()
    sku, name = _first_sku()
    snap = session_store.get(sid)
    snap.detections = [
        Detection(
            box_id="V1", x=10, y=30, w=18, h=20,
            candidates=[Candidate(sku_id=sku, name=name, score=0.4)],
            quantity=1, state="review", message="识别置信度过低",
        )
    ]
    session_store.save(snap)
    r = client.post(f"/api/v1/checkout/sessions/{sid}/confirm")
    detail = r.json().get("detail") or ""
    if r.status_code == 400 and "未确认" in detail:
        ok("未解决有候选 review → 400 未确认")
    else:
        fail("未解决有候选 review 拦截", f"status={r.status_code} detail={detail}")


def test_resolved_review_passes():
    """已人工指认（resolved）的 review → 200 CONFIRMED，不应误拦。"""
    sid = _new_session()
    sku, name = _first_sku()
    snap = session_store.get(sid)
    snap.detections = [
        Detection(
            box_id="V1", x=10, y=30, w=18, h=20,
            candidates=[Candidate(sku_id=sku, name=name, score=0.4)],
            quantity=1, state="review", message="识别置信度过低",
        )
    ]
    snap.resolved = {"V1": sku}
    session_store.save(snap)
    r = client.post(f"/api/v1/checkout/sessions/{sid}/confirm")
    if r.status_code == 200 and r.json().get("state") == "CONFIRMED":
        ok("已 resolved 的 review → 200 CONFIRMED（不误拦）")
    else:
        fail("已 resolved review 放行", f"status={r.status_code} body={r.text}")


def test_happy_path_auto_passes():
    """正常场景 S1（全 auto）→ 200 CONFIRMED，确认拦截未殃及正常结账。"""
    sid = _new_session()
    r = client.post(
        f"/api/v1/checkout/sessions/{sid}/recognize",
        json={"scene_id": "S1", "use_vision_model": False},
    )
    if r.status_code != 200:
        fail("happy-path recognize", f"status={r.status_code} body={r.text}")
        return
    r2 = client.post(f"/api/v1/checkout/sessions/{sid}/confirm")
    if r2.status_code == 200 and r2.json().get("state") == "CONFIRMED":
        ok("正常场景 S1（全 auto）→ 200 CONFIRMED")
    else:
        fail("happy-path confirm", f"status={r2.status_code} body={r2.text}")


if __name__ == "__main__":
    test_empty_detections_blocked()
    test_unresolved_empty_candidate_review_blocked()
    test_unresolved_with_candidates_review_blocked()
    test_resolved_review_passes()
    test_happy_path_auto_passes()
    print(f"\n结果: {PASSED} PASS / {FAILED} FAIL")
    if FAILED:
        print("❌ 有失败")
        sys.exit(1)
    else:
        print("✅ 全部通过")
