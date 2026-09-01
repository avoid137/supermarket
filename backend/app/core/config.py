from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（本文件往上两级）。按绝对路径加载 .env，
# 这样无论从项目根还是 backend/ 启动都能读到同一份配置。
_BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_BACKEND_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "SmartMart 无人超市 API"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---- 数据库 ----
    # 留空则使用 backend/data/smartmart.db（SQLite）
    # 切换到 PostgreSQL：postgresql+psycopg2://user:pass@localhost:5432/smartmart
    DATABASE_URL: str = ""

    # ---- 导购大模型（OpenAI 兼容协议，DeepSeek / 通义 / OpenAI 通用）----
    # 密钥一律写在 backend/.env，代码里不保留任何凭证
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-v4-flash"
    LLM_TIMEOUT: float = 60.0

    # ---- 视觉大模型（默认通义千问 Qwen-VL）----
    VISION_API_KEY: str = ""
    VISION_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    VISION_MODEL: str = "qwen-vl-max-latest"
    VISION_TIMEOUT: float = 90.0
    # 推理型模型默认会先生成大段思考再作答。本任务的本质是「从清单里选 sku_id」，
    # 不需要多步推理，实测开启思考会让单次识别从约 1 秒膨胀到 30~40 秒，
    # 且思考内容并不提升闭卷选择的准确率。默认关闭。
    VISION_ENABLE_THINKING: bool = False

    # ---- 级联检测：YOLO 出真实框 + 视觉大模型判类别 ----
    # off  = 只用视觉大模型（框是按数量均分的占位框）
    # yolo = 先用 YOLO 圈出商品位置，裁剪后再交给视觉大模型认品类
    # 级联拿不到可用结果时会自动回退到整图识别，不会让识别能力下降。
    DETECTOR: str = "off"
    YOLO_WEIGHTS: str = "models/yolov8n.pt"
    # 阈值权衡：0.30 能挡住垃圾框，但实测在「整盘多商品」场景下会把大部分
    # 组品过滤成 0~1 个框，导致一次拍照只识别出一件。降到 0.12 后框数明显
    # 提升（真实结账照 0→4 个）。低分垃圾框本身不可怕：它裁出来的图若被大模型
    # 判为清单外，级联会直接丢弃、不会入账；代价只是多一次视觉调用。
    YOLO_CONF: float = 0.12
    YOLO_MAX_BOXES: int = 8
    # 跨类非极大值抑制（NMS）的 IoU 阈值。COCO 预训练 YOLO 常把同一个物体
    # 框成两个重叠框（且被分到不同 COCO 类），而 predict 的 NMS 是按类做的、
    # 跨类不合并，于是「一个奥利奥盒子」会出两个框 → 被当成两件独立商品，
    # 用户只能二选一、还多收一次钱。这里在丢弃类别后做一遍跨类抑制，
    # 把交并比或包含度超阈值的低分框压掉，保证一个物理物体只留一个框。
    YOLO_NMS_IOU: float = 0.55

    # ---- 级联整帧补读：兜住 YOLO 漏框 ----
    # YOLO 只负责出框，弱光/小物体/密集摆放时常常只框出一两件，其余商品
    # 因没被框到而不会送去大模型、整件漏检。开启后，当一帧里 YOLO 给出的
    # 候选框 ≤ 阈值时，额外把整张图送视觉大模型读一遍——大模型本就能从
    # 整图一次列出多个商品——把漏掉的件补进账单（位置用占位框均匀铺开）。
    # 正常框数充足时不触发，避免无谓的额外视觉调用。
    CASCADE_FULLFRAME_FALLBACK: bool = True
    CASCADE_FULLFRAME_TRIGGER: int = 2

    # ---- 尺子比例尺：区分同款不同规格（演示用）----
    # 在拍摄画面里放一把尺子，每帧让视觉大模型读尺子刻度，推算 px_per_cm，
    # 再用 YOLO 框的精确像素 ÷ px_per_cm 得到商品外尺寸，与库里 pkg_*_cm 比对，
    # 在「同品牌同品类不同规格」（如乐事黄瓜味 70g / 40g）之间做长度二选一。
    # 这是演示级方案：只用「商品长度 vs 尺子比例尺」做判断，不引入复杂标定。
    RULER_ENABLED: bool = True
    # 尺子真实总长度（cm）。仅用于在日志/调试里交叉核对，不参与换算——
    # 换算用的 px_per_cm 始终来自当帧视觉模型读取的尺子刻度。
    RULER_LENGTH_CM: float = 15.0
    # 尺寸纠偏在候选排序里的权重（0~1）。0.6 表示长度贴近度最多抵消 60% 的
    # 视觉置信度差距，既让尺子能纠正明显误判，又不至于完全压过模型判断。
    RULER_LENGTH_WEIGHT: float = 0.6

    # ---- 识别阈值 ----
    AUTO_ACCEPT_SCORE: float = 0.88   # 高于此分直接入账
    CLARIFY_SCORE: float = 0.55       # 低于此分转人工复核，中间区间主动追问
    # 没有可混淆的候选时放宽阈值：商品库里找不到相似的，就不必打扰顾客
    SINGLE_CANDIDATE_SCORE: float = 0.72
    WEIGHT_TOLERANCE_G: float = 25.0  # 重量校验容差（克）
    # 模拟称重传感器时的托盘皮重（克）。演示场景下「总重量」输入框填入的
    # 通常是已去皮的净重，故默认 0；若接入真实秤且读数含托盘，可设此值自动扣除。
    TRAY_TARE_G: float = 0.0

    # ---- 后台看板鉴权 ----
    # 留空：dev 演示态全开放（任何人直访 /api/v1/admin/* 即可）
    # 设值：开启 Bearer 校验，前端需要先在 localStorage 里存 token
    ADMIN_TOKEN: str = ""

    # ---- 审计追溯 ----
    # 视觉结账的抓拍图与账单快照保留天数；清理脚本删掉过期记录
    AUDIT_RETENTION_DAYS: int = 30
    # 落盘根目录。空则用 backend/data/audit_photos
    AUDIT_PHOTO_DIR: str = ""
    # 落盘前压缩参数。原始摄像头帧约 1.5MB，按此参数压到 ~150KB
    AUDIT_MAX_DIMENSION: int = 1280
    AUDIT_JPEG_QUALITY: int = 82
    # 自动清理开关与轮询周期（小时）。设 0 关闭，仅靠手动脚本。
    # 后台 asyncio 任务在 lifespan 内启动，不抢主链路资源
    AUDIT_AUTO_PURGE: bool = True
    AUDIT_PURGE_INTERVAL_HOURS: int = 1

    # ---- 提示词 ----
    # 改完 app/prompts/*.yaml 是否免重启生效。
    # 开：每次取提示词都 stat 一次文件，mtime 变了就重新读（开发态好用）
    # 关：进程内缓存，改了要重启（生产默认，省掉每请求的磁盘 IO）
    #
    # 无论开关如何，启动时都会完整校验一遍所有 YAML——缺字段、少占位符
    # 会直接让服务起不来，而不是等到运行时才发现。
    PROMPT_HOT_RELOAD: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
