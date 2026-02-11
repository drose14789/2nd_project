"""
Plan Storage - JSON 파일 기반 플랜 저장
"""
import os
import json
import uuid
from typing import Dict, Any, Optional

STORAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "instance", "plans")


def _ensure_dir():
    os.makedirs(STORAGE_DIR, exist_ok=True)


def save_plan(plan_obj: Dict[str, Any], plan_id: Optional[str] = None) -> str:
    """플랜 저장, plan_id 반환"""
    _ensure_dir()
    
    if not plan_id:
        plan_id = str(uuid.uuid4())[:8]
    
    path = os.path.join(STORAGE_DIR, f"{plan_id}.json")
    
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan_obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Storage] Save error: {e}")
    
    return plan_id


def load_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    """플랜 로드"""
    _ensure_dir()
    
    path = os.path.join(STORAGE_DIR, f"{plan_id}.json")
    
    if not os.path.exists(path):
        return None
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Storage] Load error: {e}")
        return None


def delete_plan(plan_id: str) -> bool:
    """플랜 삭제"""
    path = os.path.join(STORAGE_DIR, f"{plan_id}.json")
    
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception as e:
        print(f"[Storage] Delete error: {e}")
    
    return False
