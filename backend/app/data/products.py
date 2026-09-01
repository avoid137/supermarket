"""门店商品主数据（演示用示例数据，非真实商品信息）。

24 个 SKU，覆盖饮料 / 零食 / 日用三个品类。
每个 SKU 同时服务两个场景：
  - 导购 Agent 的检索语料（位置、营养、过敏原、促销）
  - 视觉结账的匹配目标库（visual 字段供前端绘制示意图）
"""

from app.models.schemas import Nutrition, Product, Promotion, ShelfLocation, VisualProfile

# ---------------- 全局促销规则（不绑定单个 SKU）----------------

STORE_PROMOTIONS: list[Promotion] = [
    Promotion(
        type="threshold",
        desc="全场满 30 减 5",
        threshold_amount=30.0,
        discount=5.0,
    ),
    Promotion(
        type="bundle",
        desc="薯片 + 气泡水 组合价 ¥10.9",
        groups=[["SKU009", "SKU010"], ["SKU003", "SKU004"]],
        bundle_price=10.9,
    ),
]

# ---------------- SKU 主数据 ----------------

PRODUCTS: list[Product] = [
    # ===== 饮料 8 款 =====
    Product(
        sku_id="SKU001", name="可口可乐 汽水", brand="可口可乐", category="饮料", spec="330ml 罐装",
        price=3.5, member_price=3.2, barcode="6928804011323",
        shelf=ShelfLocation(aisle="A1", level=2, desc="进门左手边 A1 饮料架，第二层，冷藏区最外侧"),
        nutrition=Nutrition(energy_kj=180, protein_g=0, fat_g=0, carb_g=10.6, sodium_mg=4),
        ingredients="水、果葡糖浆、白砂糖、二氧化碳、焦糖色、磷酸、咖啡因、食用香精",
        allergens=[], tags=["碳酸饮料", "含咖啡因", "冷藏"],
        promotions=[Promotion(type="second_half", desc="第二件半价")],
        visual=VisualProfile(color="#C8102E", color2="#FFFFFF", shape="can", label="可乐", weight_g=355),
    ),
    Product(
        sku_id="SKU002", name="百事可乐 汽水", brand="百事", category="饮料", spec="330ml 罐装",
        price=3.5, member_price=3.2, barcode="6924748411219",
        shelf=ShelfLocation(aisle="A1", level=2, desc="A1 饮料架第二层，紧挨可口可乐右侧"),
        nutrition=Nutrition(energy_kj=190, protein_g=0, fat_g=0, carb_g=11.0, sodium_mg=5),
        ingredients="水、白砂糖、果葡糖浆、二氧化碳、焦糖色、磷酸、咖啡因、食用香精",
        allergens=[], tags=["碳酸饮料", "含咖啡因", "冷藏"],
        promotions=[Promotion(type="second_half", desc="第二件半价")],
        visual=VisualProfile(color="#004B93", color2="#E4002B", shape="can", label="百事", weight_g=355),
    ),
    Product(
        sku_id="SKU003", name="元气森林 白桃味苏打气泡水", brand="元气森林", category="饮料", spec="480ml 瓶装",
        price=5.5, member_price=4.9, barcode="6970123450017",
        shelf=ShelfLocation(aisle="A1", level=3, desc="A1 饮料架第三层左起第二排"),
        nutrition=Nutrition(energy_kj=15, protein_g=0, fat_g=0, carb_g=3.8, sodium_mg=0),
        ingredients="水、赤藓糖醇、二氧化碳、浓缩白桃汁、柠檬酸、食用香精",
        allergens=[], tags=["0糖", "0脂", "气泡水", "冷藏"],
        promotions=[],
        visual=VisualProfile(color="#F7C6D0", color2="#FFFFFF", shape="bottle", label="白桃", weight_g=500),
    ),
    Product(
        sku_id="SKU004", name="元气森林 葡萄味苏打气泡水", brand="元气森林", category="饮料", spec="480ml 瓶装",
        price=5.5, member_price=4.9,
        shelf=ShelfLocation(aisle="A1", level=3, desc="A1 饮料架第三层，白桃味右侧相邻"),
        nutrition=Nutrition(energy_kj=15, protein_g=0, fat_g=0, carb_g=3.9, sodium_mg=0),
        ingredients="水、赤藓糖醇、二氧化碳、浓缩葡萄汁、柠檬酸、食用香精",
        allergens=[], tags=["0糖", "0脂", "气泡水", "冷藏"],
        promotions=[],
        visual=VisualProfile(color="#8E6BBF", color2="#FFFFFF", shape="bottle", label="葡萄", weight_g=500),
    ),
    Product(
        sku_id="SKU005", name="农夫山泉 饮用天然水", brand="农夫山泉", category="饮料", spec="550ml 瓶装",
        price=2.0, member_price=1.8, barcode="6921168509256",
        shelf=ShelfLocation(aisle="A1", level=1, desc="A1 饮料架最底层，整箱堆放区"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=0.8),
        ingredients="天然水",
        allergens=[], tags=["饮用水", "常温", "低价"],
        promotions=[Promotion(type="second_half", desc="第二件半价")],
        visual=VisualProfile(color="#E8F4F8", color2="#D6323A", shape="bottle", label="泉水", weight_g=570),
    ),
    Product(
        sku_id="SKU006", name="东方树叶 乌龙茶", brand="东方树叶", category="饮料", spec="500ml 瓶装",
        price=5.0, member_price=4.5,
        shelf=ShelfLocation(aisle="A1", level=3, desc="A1 饮料架第三层最右端，茶饮料区"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=5),
        ingredients="水、乌龙茶茶叶、维生素C、碳酸氢钠",
        allergens=[], tags=["无糖茶", "0卡", "常温"],
        promotions=[],
        visual=VisualProfile(color="#C7D8A0", color2="#FFFFFF", shape="bottle", label="乌龙", weight_g=520),
    ),
    Product(
        sku_id="SKU007", name="特仑苏 纯牛奶", brand="特仑苏", category="饮料", spec="250ml 盒装",
        price=6.5, member_price=5.9,
        shelf=ShelfLocation(aisle="A1", level=1, desc="A1 饮料架底层冷柜，乳制品专区"),
        nutrition=Nutrition(energy_kj=280, protein_g=3.3, fat_g=3.6, carb_g=4.9, sodium_mg=50),
        ingredients="生牛乳",
        allergens=["牛奶"], tags=["高蛋白", "乳制品", "冷藏"],
        promotions=[],
        visual=VisualProfile(color="#F2E8D5", color2="#0F4C81", shape="box", label="牛奶", weight_g=260),
    ),
    Product(
        sku_id="SKU008", name="三顿半 精品速溶咖啡", brand="三顿半", category="饮料", spec="3g 小杯装",
        price=8.0, member_price=7.2,
        shelf=ShelfLocation(aisle="A1", level=2, desc="A1 饮料架第二层右端，咖啡专区"),
        nutrition=Nutrition(energy_kj=1400, protein_g=12.0, fat_g=0.5, carb_g=70.0, sodium_mg=30),
        ingredients="阿拉比卡咖啡豆冻干粉",
        allergens=[], tags=["咖啡", "提神", "常温"],
        promotions=[],
        visual=VisualProfile(color="#6F4E37", color2="#F5E6D3", shape="cup", label="咖啡", weight_g=10),
    ),

    # ===== 零食 10 款 =====
    Product(
        sku_id="SKU009", name="乐事 黄瓜味薯片", brand="乐事", category="零食", spec="70g 袋装",
        price=6.5, member_price=5.9, barcode="6924748411318",
        shelf=ShelfLocation(aisle="A2", level=2, desc="A2 零食架第二层，薯片区左起第一"),
        nutrition=Nutrition(energy_kj=2200, protein_g=6.0, fat_g=33.0, carb_g=55.0, sodium_mg=600),
        ingredients="马铃薯、植物油、黄瓜味调味料、白砂糖、食用盐",
        allergens=["小麦", "牛奶"], tags=["膨化食品", "高钠", "素食"],
        promotions=[],
        visual=VisualProfile(color="#7CB342", color2="#FFFFFF", shape="bag", label="黄瓜", weight_g=75,
                             pkg_width_cm=17.0, pkg_length_cm=22.0),
    ),
    Product(
        sku_id="SKU010", name="乐事 原味薯片", brand="乐事", category="零食", spec="70g 袋装",
        price=6.5, member_price=5.9, barcode="6924748411325",
        shelf=ShelfLocation(aisle="A2", level=2, desc="A2 零食架第二层，黄瓜味右侧相邻"),
        nutrition=Nutrition(energy_kj=2250, protein_g=6.2, fat_g=34.0, carb_g=54.0, sodium_mg=580),
        ingredients="马铃薯、植物油、食用盐",
        allergens=[], tags=["膨化食品", "高钠", "素食"],
        promotions=[],
        visual=VisualProfile(color="#F5D76E", color2="#E8B92A", shape="bag", label="原味", weight_g=75,
                             pkg_width_cm=17.0, pkg_length_cm=22.0),
    ),
    Product(
        sku_id="SKU011", name="奥利奥 夹心饼干 原味", brand="奥利奥", category="零食", spec="116g 卷装",
        price=7.5, member_price=6.8,
        shelf=ShelfLocation(aisle="A2", level=3, desc="A2 零食架第三层，饼干区中部"),
        nutrition=Nutrition(energy_kj=2000, protein_g=4.5, fat_g=20.0, carb_g=68.0, sodium_mg=350),
        ingredients="小麦粉、白砂糖、植物油、可可粉、乳清粉、大豆磷脂",
        allergens=["小麦", "大豆", "牛奶"], tags=["饼干", "甜食", "含可可"],
        promotions=[Promotion(type="second_half", desc="第二件半价")],
        visual=VisualProfile(color="#2C2C2C", color2="#FFFFFF", shape="pack", label="奥利奥", weight_g=125),
    ),
    Product(
        sku_id="SKU012", name="好丽友 巧克力派", brand="好丽友", category="零食", spec="6 枚装",
        price=12.0, member_price=10.9,
        shelf=ShelfLocation(aisle="A2", level=3, desc="A2 零食架第三层，派类糕点区"),
        nutrition=Nutrition(energy_kj=1800, protein_g=5.0, fat_g=17.0, carb_g=62.0, sodium_mg=300),
        ingredients="小麦粉、白砂糖、鸡蛋、植物油、可可粉、乳粉",
        allergens=["小麦", "鸡蛋", "牛奶"], tags=["糕点", "甜食", "含可可"],
        promotions=[],
        visual=VisualProfile(color="#8B4513", color2="#D2B48C", shape="box", label="好丽友", weight_g=180),
    ),
    Product(
        sku_id="SKU013", name="三只松鼠 每日坚果", brand="三只松鼠", category="零食", spec="25g × 7 袋",
        price=39.9, member_price=35.9,
        shelf=ShelfLocation(aisle="A2", level=1, desc="A2 零食架底层，礼盒与坚果专区"),
        nutrition=Nutrition(energy_kj=2500, protein_g=12.0, fat_g=45.0, carb_g=30.0, sodium_mg=200),
        ingredients="核桃仁、腰果仁、扁桃仁、蔓越莓干、蓝莓干、榛子仁",
        allergens=["坚果"], tags=["坚果", "高蛋白", "高脂", "礼盒"],
        promotions=[],
        visual=VisualProfile(color="#D4A017", color2="#8B5A2B", shape="box", label="坚果", weight_g=190),
    ),
    Product(
        sku_id="SKU014", name="卫龙 亲嘴烧 麻辣味", brand="卫龙", category="零食", spec="108g 袋装",
        price=5.5, member_price=4.9,
        shelf=ShelfLocation(aisle="A2", level=2, desc="A2 零食架第二层右端，辣条专区"),
        nutrition=Nutrition(energy_kj=1600, protein_g=6.0, fat_g=14.0, carb_g=55.0, sodium_mg=2000),
        ingredients="小麦粉、植物油、辣椒、食用盐、白砂糖、大豆蛋白",
        allergens=["小麦", "大豆"], tags=["辣味", "高钠", "素食"],
        promotions=[],
        visual=VisualProfile(color="#D93A2B", color2="#FFD700", shape="bag", label="辣条", weight_g=115),
    ),
    Product(
        sku_id="SKU015", name="士力架 花生夹心巧克力", brand="士力架", category="零食", spec="51g 条装",
        price=4.5, member_price=4.0,
        shelf=ShelfLocation(aisle="A2", level=3, desc="A2 零食架第三层收银台侧，巧克力货架"),
        nutrition=Nutrition(energy_kj=2100, protein_g=8.0, fat_g=25.0, carb_g=60.0, sodium_mg=200),
        ingredients="牛奶巧克力、花生、葡萄糖浆、白砂糖、乳粉、可可脂",
        allergens=["花生", "牛奶", "大豆"], tags=["巧克力", "高热量", "横扫饥饿"],
        promotions=[],
        visual=VisualProfile(color="#5B3A1A", color2="#C8102E", shape="pack", label="士力架", weight_g=55),
    ),
    Product(
        sku_id="SKU016", name="徐福记 鸡蛋味沙琪玛", brand="徐福记", category="零食", spec="400g 袋装",
        price=15.9, member_price=14.5,
        shelf=ShelfLocation(aisle="A2", level=1, desc="A2 零食架底层，中式糕点区"),
        nutrition=Nutrition(energy_kj=1900, protein_g=5.0, fat_g=20.0, carb_g=62.0, sodium_mg=150),
        ingredients="小麦粉、鸡蛋、白砂糖、植物油、麦芽糖浆",
        allergens=["小麦", "鸡蛋"], tags=["糕点", "甜食", "家庭装"],
        promotions=[],
        visual=VisualProfile(color="#F0C96B", color2="#FFF3D6", shape="bag", label="沙琪玛", weight_g=420),
    ),
    Product(
        sku_id="SKU017", name="炫迈 无糖口香糖 薄荷味", brand="炫迈", category="零食", spec="40 粒瓶装",
        price=9.9, member_price=8.9,
        shelf=ShelfLocation(aisle="A2", level=3, desc="A2 零食架第三层最右端，口香糖小货架"),
        nutrition=Nutrition(energy_kj=700, protein_g=0, fat_g=0, carb_g=70.0, sodium_mg=10),
        ingredients="木糖醇、山梨糖醇、胶基、薄荷香料、阿斯巴甜",
        allergens=[], tags=["无糖", "口气清新", "含甜味剂"],
        promotions=[],
        visual=VisualProfile(color="#4FC3F7", color2="#FFFFFF", shape="bottle", label="口香糖", weight_g=65),
    ),
    Product(
        sku_id="SKU018", name="蒙牛 随变 香草味雪糕", brand="蒙牛", category="零食", spec="65g 支装",
        price=5.0, member_price=4.5, barcode="6923648611271",
        shelf=ShelfLocation(aisle="A1", level=1, desc="A1 饮料架底层冷柜最里侧，冰品专区"),
        nutrition=Nutrition(energy_kj=900, protein_g=3.0, fat_g=12.0, carb_g=25.0, sodium_mg=60),
        ingredients="生牛乳、白砂糖、植物油、香草香精、乳粉",
        allergens=["牛奶"], tags=["冰品", "冷藏", "甜食"],
        promotions=[],
        visual=VisualProfile(color="#FFF3E0", color2="#8B4513", shape="pack", label="雪糕", weight_g=70),
    ),

    # ===== 日用 6 款 =====
    Product(
        sku_id="SKU019", name="清风 原木纯品抽纸", brand="清风", category="日用", spec="3 层 120 抽 × 3 包",
        price=9.9, member_price=8.9,
        shelf=ShelfLocation(aisle="A3", level=1, desc="A3 日用架底层，纸品区整提堆放"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=0),
        ingredients="原生木浆",
        allergens=[], tags=["纸品", "非食品", "家庭装"],
        promotions=[],
        visual=VisualProfile(color="#F5F5DC", color2="#4CAF50", shape="pack", label="抽纸", weight_g=300),
    ),
    Product(
        sku_id="SKU020", name="云南白药 益优清爽型牙膏", brand="云南白药", category="日用", spec="120g 支装",
        price=19.9, member_price=17.9,
        shelf=ShelfLocation(aisle="A3", level=2, desc="A3 日用架第二层，口腔护理区"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=0),
        ingredients="水合硅石、山梨醇、云南白药活性成分、薄荷提取物、氟化钠",
        allergens=[], tags=["口腔护理", "非食品", "含氟"],
        promotions=[],
        visual=VisualProfile(color="#2E7D32", color2="#FFFFFF", shape="tube", label="牙膏", weight_g=140),
    ),
    Product(
        sku_id="SKU021", name="蓝月亮 深层洁净洗衣液", brand="蓝月亮", category="日用", spec="1kg 瓶装",
        price=29.9, member_price=26.9,
        shelf=ShelfLocation(aisle="A3", level=1, desc="A3 日用架底层最右侧，洗涤专区"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=0),
        ingredients="水、表面活性剂、酶制剂、香精、防腐剂",
        allergens=[], tags=["洗涤", "非食品", "大瓶装"],
        promotions=[],
        visual=VisualProfile(color="#1976D2", color2="#FFFFFF", shape="bottle", label="洗衣液", weight_g=1080),
    ),
    Product(
        sku_id="SKU022", name="3M KN95 防尘口罩", brand="3M", category="日用", spec="5 只装",
        price=25.0, member_price=22.5,
        shelf=ShelfLocation(aisle="A3", level=3, desc="A3 日用架第三层，防护与医药区"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=0),
        ingredients="无纺布、熔喷布、鼻夹、耳带",
        allergens=[], tags=["防护", "非食品", "KN95"],
        promotions=[],
        visual=VisualProfile(color="#B0BEC5", color2="#FFFFFF", shape="box", label="口罩", weight_g=60),
    ),
    Product(
        sku_id="SKU023", name="滴露 衣物除菌液", brand="滴露", category="日用", spec="1.5L 瓶装",
        price=39.9, member_price=35.9,
        shelf=ShelfLocation(aisle="A3", level=1, desc="A3 日用架底层，洗衣液左侧相邻"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=0),
        ingredients="水、对氯间二甲苯酚、表面活性剂",
        allergens=[], tags=["除菌", "非食品", "大瓶装"],
        promotions=[],
        visual=VisualProfile(color="#00897B", color2="#FFFFFF", shape="bottle", label="除菌液", weight_g=1600),
    ),
    Product(
        sku_id="SKU024", name="舒肤佳 纯白清香型香皂", brand="舒肤佳", category="日用", spec="115g 块装",
        price=6.9, member_price=6.2,
        shelf=ShelfLocation(aisle="A3", level=2, desc="A3 日用架第二层，洗护清洁区"),
        nutrition=Nutrition(energy_kj=0, protein_g=0, fat_g=0, carb_g=0, sodium_mg=0),
        ingredients="皂基、甘油、香精、抑菌成分",
        allergens=[], tags=["洗护", "非食品", "除菌"],
        promotions=[],
        visual=VisualProfile(color="#EF5350", color2="#FFFFFF", shape="box", label="香皂", weight_g=120),
    ),

    # ===== 识别测试新增 4 款（真实商品数据） =====
    # 数据来源：品牌官网 / 百度百科 / 京东商品页 / 进口商商品页，营养值按包装标示换算
    Product(
        # 营养值来源：中国产 Oreo 原味 97g 包装（每 100g：2027kJ、蛋白 4.8g、脂肪 21.1g、碳水 67.8g、盐 0.36g）
        # 钠 144mg 由标示的盐 0.36g 换算（钠 ≈ 盐 × 400）
        sku_id="SKU025", name="奥利奥 夹心饼干 原味", brand="奥利奥", category="零食", spec="97g 盒装（内含两小包）",
        price=6.5, member_price=5.9,
        shelf=ShelfLocation(aisle="A2", level=3, desc="A2 零食架第三层，饼干区紧邻 116g 卷装右侧"),
        nutrition=Nutrition(energy_kj=2027, protein_g=4.8, fat_g=21.1, carb_g=67.8, sodium_mg=144),
        ingredients="小麦粉、白砂糖、植物油、可可粉（添加量不低于 4.4%）、淀粉、食品添加剂（碳酸氢钠、碳酸氢铵、大豆磷脂、柠檬酸）、食盐、食用香精",
        allergens=["小麦", "大豆", "牛奶", "芝麻", "鸡蛋"], tags=["饼干", "甜食", "含可可", "便携装"],
        promotions=[Promotion(type="second_half", desc="第二件半价")],
        visual=VisualProfile(color="#1A237E", color2="#FFFFFF", shape="box", label="奥利奥97g", weight_g=110),
    ),
    Product(
        # 营养值来源：百度百科水溶C100 词条（每瓶 445ml：710kJ、碳水 42.0g、钠 120mg、维生素C 100mg），按每 100ml 换算
        sku_id="SKU026", name="农夫山泉 水溶C100 柠檬味复合果汁饮料", brand="农夫山泉", category="饮料", spec="445ml 瓶装",
        price=5.5, member_price=5.0,
        shelf=ShelfLocation(aisle="A1", level=2, desc="A1 饮料架第二层，果汁区左起第一"),
        nutrition=Nutrition(energy_kj=160, protein_g=0, fat_g=0, carb_g=9.4, sodium_mg=27),
        ingredients="水、白砂糖、果葡糖浆、浓缩柠檬汁、浓缩苹果汁（果汁含量 12%，每瓶含维生素C 100mg）",
        allergens=[], tags=["果汁", "维生素C", "含糖", "常温"],
        promotions=[],
        visual=VisualProfile(color="#FFD600", color2="#FFFFFF", shape="bottle", label="水溶C100", weight_g=475),
    ),
    Product(
        # 营养值来源：京东乐事薯片 40g 袋装经典原味包装（每份 40g：916kJ、蛋白 2.3g、脂肪 13.3g、钠 NRV 11%），按每 100g 换算
        # 碳水为按同系列配方推算值，包装未直接标示
        sku_id="SKU027", name="乐事 原味薯片", brand="乐事", category="零食", spec="40g 袋装",
        price=4.0, member_price=3.6,
        shelf=ShelfLocation(aisle="A2", level=2, desc="A2 零食架第二层，与原味 70g 同排，小规格区左端"),
        nutrition=Nutrition(energy_kj=2290, protein_g=5.8, fat_g=33.3, carb_g=53.0, sodium_mg=550),
        ingredients="马铃薯、植物油、经典原味调味料（含 5'-呈味核苷酸二钠）、TBHQ",
        allergens=[], tags=["膨化食品", "高钠", "素食", "小包装"],
        promotions=[],
        visual=VisualProfile(color="#F5D76E", color2="#E8B92A", shape="bag", label="原味40g", weight_g=45,
                             pkg_width_cm=14.0, pkg_length_cm=20.0),
    ),
    Product(
        # 与 SKU009 同为「乐事 黄瓜味薯片」，仅规格不同（40g vs 70g），用于演示
        # 「尺子比例尺」区分同款不同规格。营养值按京东乐事薯片 40g 袋装原味配方同系列推算，
        # 碳水为估算值；外尺寸 pkg_width_cm/pkg_length_cm 为实测铺平后的长宽（cm）。
        sku_id="SKU029", name="乐事 黄瓜味薯片", brand="乐事", category="零食", spec="40g 袋装",
        price=4.0, member_price=3.6,
        shelf=ShelfLocation(aisle="A2", level=2, desc="A2 零食架第二层，与黄瓜味 70g 同排，小规格区"),
        nutrition=Nutrition(energy_kj=2200, protein_g=6.0, fat_g=33.0, carb_g=55.0, sodium_mg=600),
        ingredients="马铃薯、植物油、黄瓜味调味料、白砂糖、食用盐",
        allergens=["小麦", "牛奶"], tags=["膨化食品", "高钠", "素食", "小包装"],
        promotions=[],
        visual=VisualProfile(color="#7CB342", color2="#FFFFFF", shape="bag", label="黄瓜40g", weight_g=45,
                             pkg_width_cm=14.0, pkg_length_cm=20.0),
    ),
    Product(
        # 参数来源：崂山啤酒 500ml（原麦汁浓度 10°P，酒精度 4.0%vol，配料为水、麦芽、大米、啤酒花）
        # 营养值为 10°P 拉格啤酒的行业典型值估算，非包装标示值
        sku_id="SKU028", name="崂山啤酒", brand="崂山", category="饮料", spec="500ml 瓶装",
        price=4.5, member_price=4.0, barcode="6924758411339",
        shelf=ShelfLocation(aisle="A1", level=1, desc="A1 饮料架底层，啤酒区玻璃瓶装专位"),
        nutrition=Nutrition(energy_kj=150, protein_g=0.4, fat_g=0, carb_g=3.2, sodium_mg=12),
        ingredients="水、麦芽、大米、啤酒花（原麦汁浓度 10°P，酒精度 4.0%vol）",
        allergens=["大麦"], tags=["啤酒", "含酒精", "玻璃瓶", "含麸质"],
        promotions=[],
        visual=VisualProfile(color="#2E7D32", color2="#FFFFFF", shape="bottle", label="崂山", weight_g=790),
    ),
]

PRODUCT_MAP: dict[str, Product] = {p.sku_id: p for p in PRODUCTS}

# 相似商品分组：用于视觉仲裁阶段判定歧义
SIMILAR_GROUPS: list[list[str]] = [
    ["SKU003", "SKU004"],  # 元气森林白桃 / 葡萄，包装仅颜色不同
    ["SKU009", "SKU010", "SKU027", "SKU029"],  # 乐事黄瓜 70g / 原味 70g / 原味 40g / 黄瓜 40g，同口味不同规格为主
    ["SKU001", "SKU002"],  # 可口 / 百事
    ["SKU011", "SKU025"],  # 奥利奥 116g 卷装 / 97g 盒装，同口味不同包装形态
]
