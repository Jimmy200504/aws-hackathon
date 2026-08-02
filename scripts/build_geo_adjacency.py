#!/usr/bin/env python3
"""Author the geographic adjacency graph, so the behaviour graph has something to disagree with.

`artifacts/district-graph.json` answers "which districts do job seekers treat as
interchangeable". That is not the same question as "which districts touch", and
the interesting output of this repo's geo work has consistently been the gap
between the two:

  adjacent and substitutable       ordinary neighbours
  adjacent, not substitutable      a barrier - a mountain, an unbridged river,
                                   or two districts that simply face away from
                                   each other
  substitutable, not adjacent      a corridor - a rail line, a freeway, a
                                   tunnel that makes distance irrelevant

Neither graph can produce those categories alone. The behaviour graph does not
know what a mountain is; a map does not know that 基隆 commuters work in 台北.

This file is hand-authored from geography, which means it is the least
trustworthy artifact in the repo and is treated that way:
`scripts/validate_geo_adjacency.py` scores it against the behaviour graph and
`app/geo_graph.py` loads it with `provenance: authored`, separately ablatable
from every behaviour edge.

Adjacency is land-border adjacency between the 368 official districts. Islands
have no land neighbours and are recorded with their ferry or bridge link
instead, because "no neighbours" and "reachable only by boat" are different
facts and the second one is the one a job seeker feels.

`commute` grades how hard the border actually is to cross, which adjacency
alone does not say. 八里 and 淡水 face each other across the 淡水河 mouth and
were a 15km detour until 淡江大橋 opened on 2026-05-12; 坪林 and 頭城 are
separated by the 雪山山脈 and joined by a 12.9km tunnel. Both pairs are
"adjacent"; neither is an ordinary walk across a boundary.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_L4 = ROOT / "config" / "geo-l4-districts.json"
DEFAULT_OUTPUT = ROOT / "config" / "geo-adjacency.json"

# ---------------------------------------------------------------- intra-county
# district -> neighbours inside the same county. Written as adjacency lists and
# de-duplicated into undirected pairs at build time, so a pair stated once from
# either side is enough and stating it twice is harmless.
INTRA: dict[str, dict[str, list[str]]] = {
    "台北市": {
        "中正區": ["大同區", "中山區", "大安區", "萬華區", "文山區"],
        "大同區": ["中山區", "萬華區", "士林區"],
        "中山區": ["松山區", "大安區", "內湖區", "士林區"],
        "松山區": ["大安區", "信義區", "南港區", "內湖區"],
        "大安區": ["信義區", "文山區"],
        "信義區": ["南港區", "文山區"],
        "士林區": ["北投區", "內湖區"],
        "內湖區": ["南港區"],
        "南港區": ["文山區"],
    },
    "新北市": {
        "萬里區": ["金山區"],
        "金山區": ["石門區", "三芝區"],
        "石門區": ["三芝區"],
        "三芝區": ["淡水區"],
        "淡水區": ["八里區"],
        "八里區": ["五股區", "林口區"],
        "林口區": ["五股區", "泰山區"],
        "五股區": ["泰山區", "新莊區", "三重區", "蘆洲區"],
        "泰山區": ["新莊區"],
        "新莊區": ["三重區", "板橋區", "樹林區"],
        "三重區": ["蘆洲區", "板橋區"],
        "板橋區": ["樹林區", "土城區", "中和區", "永和區"],
        "樹林區": ["土城區", "三峽區", "鶯歌區"],
        "鶯歌區": ["三峽區"],
        "三峽區": ["土城區", "烏來區"],
        "土城區": ["中和區", "新店區"],
        "中和區": ["永和區", "新店區"],
        "新店區": ["深坑區", "石碇區", "坪林區", "烏來區"],
        "深坑區": ["石碇區"],
        "石碇區": ["坪林區", "平溪區", "汐止區"],
        "坪林區": ["雙溪區"],
        "汐止區": ["平溪區", "瑞芳區"],
        "平溪區": ["瑞芳區", "雙溪區"],
        "瑞芳區": ["雙溪區", "貢寮區"],
        "雙溪區": ["貢寮區"],
    },
    "基隆市": {
        "仁愛區": ["信義區", "中正區", "中山區", "安樂區"],
        "信義區": ["中正區", "暖暖區", "安樂區"],
        "中正區": ["中山區"],
        "中山區": ["安樂區"],
        "安樂區": ["七堵區"],
        "暖暖區": ["七堵區"],
    },
    "桃園市": {
        "桃園區": ["龜山區", "八德區", "蘆竹區", "大園區", "中壢區"],
        "龜山區": ["八德區", "蘆竹區"],
        "蘆竹區": ["大園區"],
        "大園區": ["中壢區", "觀音區"],
        "中壢區": ["觀音區", "新屋區", "楊梅區", "平鎮區", "八德區"],
        "平鎮區": ["楊梅區", "龍潭區", "八德區"],
        "八德區": ["大溪區"],
        "大溪區": ["龍潭區", "復興區"],
        "龍潭區": ["楊梅區"],
        "楊梅區": ["新屋區"],
        "新屋區": ["觀音區"],
    },
    "新竹市": {
        "東區": ["北區", "香山區"],
        "北區": ["香山區"],
    },
    "新竹縣": {
        "竹北市": ["湖口鄉", "新豐鄉", "新埔鎮", "芎林鄉", "竹東鎮"],
        "湖口鄉": ["新豐鄉", "新埔鎮"],
        "新埔鎮": ["關西鎮", "芎林鄉"],
        "關西鎮": ["芎林鄉", "橫山鄉", "尖石鄉"],
        "芎林鄉": ["橫山鄉", "竹東鎮"],
        "橫山鄉": ["尖石鄉", "竹東鎮", "北埔鄉"],
        "尖石鄉": ["五峰鄉"],
        "竹東鎮": ["北埔鄉", "寶山鄉", "五峰鄉"],
        "寶山鄉": ["北埔鄉"],
        "北埔鄉": ["峨眉鄉", "五峰鄉"],
        "峨眉鄉": ["五峰鄉"],
    },
    "苗栗縣": {
        "竹南鎮": ["頭份市", "造橋鄉", "後龍鎮"],
        "頭份市": ["造橋鄉", "三灣鄉"],
        "三灣鄉": ["造橋鄉", "南庄鄉", "獅潭鄉"],
        "南庄鄉": ["獅潭鄉", "泰安鄉"],
        "獅潭鄉": ["大湖鄉", "公館鄉", "頭屋鄉", "造橋鄉", "泰安鄉"],
        "後龍鎮": ["造橋鄉", "西湖鄉", "通霄鎮", "苗栗市"],
        "通霄鎮": ["西湖鄉", "苑裡鎮", "銅鑼鄉", "三義鄉"],
        "苑裡鎮": ["三義鄉"],
        "苗栗市": ["頭屋鄉", "公館鄉", "銅鑼鄉", "西湖鄉", "造橋鄉"],
        "造橋鄉": ["頭屋鄉"],
        "頭屋鄉": ["公館鄉"],
        "公館鄉": ["大湖鄉", "銅鑼鄉"],
        "大湖鄉": ["泰安鄉", "卓蘭鎮", "銅鑼鄉", "三義鄉"],
        "泰安鄉": ["卓蘭鎮"],
        "銅鑼鄉": ["三義鄉", "西湖鄉"],
        "三義鄉": ["卓蘭鎮"],
    },
    "台中市": {
        "中區": ["東區", "南區", "西區", "北區"],
        "東區": ["北區", "南區", "太平區", "北屯區"],
        "南區": ["西區", "南屯區", "大里區", "烏日區"],
        "西區": ["北區", "南屯區", "西屯區"],
        "北區": ["北屯區", "西屯區"],
        "北屯區": ["西屯區", "太平區", "潭子區", "大雅區", "新社區"],
        "西屯區": ["南屯區", "大雅區", "沙鹿區", "龍井區"],
        "南屯區": ["烏日區", "大肚區", "龍井區"],
        "太平區": ["大里區", "霧峰區", "新社區"],
        "大里區": ["霧峰區", "烏日區"],
        "霧峰區": ["烏日區"],
        "烏日區": ["大肚區"],
        "豐原區": ["后里區", "石岡區", "神岡區", "潭子區", "新社區"],
        "后里區": ["神岡區", "外埔區", "石岡區"],
        "石岡區": ["東勢區", "新社區"],
        "東勢區": ["新社區", "和平區"],
        "和平區": ["新社區"],
        "潭子區": ["大雅區", "神岡區"],
        "大雅區": ["神岡區", "沙鹿區", "清水區"],
        "神岡區": ["清水區", "外埔區"],
        "大肚區": ["龍井區", "沙鹿區"],
        "沙鹿區": ["龍井區", "梧棲區", "清水區"],
        "龍井區": ["梧棲區"],
        "梧棲區": ["清水區"],
        "清水區": ["大甲區", "外埔區", "大安區"],
        "大甲區": ["大安區", "外埔區"],
        "外埔區": ["大安區"],
    },
    "彰化縣": {
        "彰化市": ["花壇鄉", "和美鎮", "秀水鄉", "芬園鄉"],
        "芬園鄉": ["花壇鄉", "大村鄉"],
        "花壇鄉": ["大村鄉", "秀水鄉", "員林市"],
        "秀水鄉": ["鹿港鎮", "福興鄉", "埔鹽鄉", "和美鎮"],
        "鹿港鎮": ["福興鄉", "和美鎮", "線西鄉", "埔鹽鄉"],
        "福興鄉": ["埔鹽鄉", "芳苑鄉", "二林鎮"],
        "線西鄉": ["和美鎮", "伸港鄉"],
        "和美鎮": ["伸港鄉"],
        "員林市": ["大村鄉", "社頭鄉", "永靖鄉", "埔心鄉"],
        "社頭鄉": ["田中鎮", "永靖鄉", "田尾鄉"],
        "永靖鄉": ["埔心鄉", "田尾鄉"],
        "埔心鄉": ["溪湖鎮", "大村鄉", "埔鹽鄉"],
        "溪湖鎮": ["埔鹽鄉", "二林鎮", "埤頭鄉", "田尾鄉"],
        "田中鎮": ["田尾鄉", "北斗鎮", "二水鄉"],
        "北斗鎮": ["田尾鄉", "溪州鄉", "埤頭鄉"],
        "田尾鄉": ["埤頭鄉"],
        "埤頭鄉": ["溪州鄉", "竹塘鄉", "二林鎮"],
        "溪州鄉": ["竹塘鄉", "二水鄉"],
        "竹塘鄉": ["二林鎮", "大城鄉"],
        "二林鎮": ["大城鄉", "芳苑鄉"],
        "大城鄉": ["芳苑鄉"],
    },
    "南投縣": {
        "南投市": ["中寮鄉", "草屯鎮", "名間鄉"],
        "中寮鄉": ["草屯鎮", "國姓鄉", "水里鄉", "集集鎮", "名間鄉"],
        "草屯鎮": ["國姓鄉"],
        "國姓鄉": ["埔里鎮"],
        "埔里鎮": ["仁愛鄉", "魚池鄉"],
        "仁愛鄉": ["信義鄉"],
        "名間鄉": ["集集鎮", "竹山鎮"],
        "集集鎮": ["水里鄉", "魚池鄉", "鹿谷鄉", "竹山鎮"],
        "水里鄉": ["魚池鄉", "信義鄉", "鹿谷鄉"],
        "魚池鄉": ["信義鄉"],
        "信義鄉": ["鹿谷鄉"],
        "竹山鎮": ["鹿谷鄉"],
    },
    "雲林縣": {
        "斗六市": ["斗南鎮", "古坑鄉", "林內鄉", "莿桐鄉"],
        "斗南鎮": ["大埤鄉", "古坑鄉", "虎尾鎮"],
        "古坑鄉": ["大埤鄉"],
        "林內鄉": ["莿桐鄉"],
        "莿桐鄉": ["西螺鎮", "虎尾鎮"],
        "西螺鎮": ["二崙鄉", "虎尾鎮"],
        "二崙鄉": ["崙背鄉", "虎尾鎮"],
        "崙背鄉": ["麥寮鄉", "土庫鎮", "褒忠鄉"],
        "麥寮鄉": ["台西鄉", "褒忠鄉"],
        "台西鄉": ["褒忠鄉", "東勢鄉", "四湖鄉"],
        "東勢鄉": ["褒忠鄉", "土庫鎮", "四湖鄉", "元長鄉"],
        "褒忠鄉": ["土庫鎮"],
        "土庫鎮": ["虎尾鎮", "元長鄉"],
        "虎尾鎮": ["元長鄉", "大埤鄉"],
        "元長鄉": ["四湖鄉", "北港鎮"],
        "四湖鄉": ["口湖鄉", "水林鄉"],
        "口湖鄉": ["水林鄉"],
        "水林鄉": ["北港鎮"],
    },
    "嘉義市": {"東區": ["西區"]},
    "嘉義縣": {
        "番路鄉": ["竹崎鄉", "中埔鄉", "阿里山鄉", "梅山鄉"],
        "梅山鄉": ["竹崎鄉", "大林鎮", "阿里山鄉"],
        "竹崎鄉": ["民雄鄉", "中埔鄉", "阿里山鄉", "大林鎮"],
        "阿里山鄉": ["大埔鄉"],
        "中埔鄉": ["水上鄉", "大埔鄉"],
        "水上鄉": ["鹿草鄉", "太保市"],
        "鹿草鄉": ["太保市", "朴子市", "義竹鄉", "六腳鄉"],
        "太保市": ["朴子市", "六腳鄉", "民雄鄉", "新港鄉"],
        "朴子市": ["東石鄉", "六腳鄉", "義竹鄉"],
        "東石鄉": ["六腳鄉", "布袋鎮", "義竹鄉"],
        "六腳鄉": ["新港鄉"],
        "新港鄉": ["民雄鄉", "溪口鄉"],
        "民雄鄉": ["溪口鄉", "大林鎮"],
        "大林鎮": ["溪口鄉"],
        "義竹鄉": ["布袋鎮"],
    },
    "台南市": {
        "中西區": ["東區", "南區", "北區", "安平區"],
        "東區": ["南區", "北區", "永康區", "仁德區"],
        "南區": ["安平區", "仁德區"],
        "北區": ["安南區", "永康區"],
        "安平區": ["安南區"],
        "安南區": ["永康區", "西港區", "七股區", "安定區"],
        "永康區": ["仁德區", "歸仁區", "新化區", "新市區", "安定區"],
        "仁德區": ["歸仁區"],
        "歸仁區": ["新化區", "關廟區"],
        "關廟區": ["新化區", "龍崎區", "仁德區"],
        "龍崎區": ["新化區"],
        "新化區": ["新市區", "山上區", "左鎮區"],
        "左鎮區": ["山上區", "玉井區", "南化區", "大內區"],
        "玉井區": ["南化區", "楠西區", "大內區"],
        "楠西區": ["南化區", "大內區"],
        "南化區": [],
        "大內區": ["山上區", "善化區", "官田區"],
        "山上區": ["善化區", "新市區"],
        "新市區": ["善化區", "安定區"],
        "善化區": ["官田區", "安定區", "麻豆區"],
        "安定區": ["西港區", "麻豆區"],
        "西港區": ["七股區", "佳里區", "麻豆區"],
        "七股區": ["佳里區", "將軍區"],
        "佳里區": ["將軍區", "學甲區", "麻豆區"],
        "將軍區": ["學甲區", "北門區"],
        "學甲區": ["北門區", "下營區", "鹽水區", "麻豆區"],
        "北門區": ["鹽水區"],
        "麻豆區": ["下營區", "官田區"],
        "官田區": ["下營區", "六甲區"],
        "下營區": ["六甲區", "鹽水區", "新營區", "柳營區"],
        "六甲區": ["柳營區", "東山區"],
        "柳營區": ["新營區", "東山區"],
        "新營區": ["鹽水區", "後壁區", "東山區"],
        "鹽水區": ["後壁區"],
        "後壁區": ["白河區", "東山區"],
        "白河區": ["東山區"],
    },
    "高雄市": {
        "新興區": ["前金區", "苓雅區", "三民區"],
        "前金區": ["苓雅區", "鹽埕區", "三民區"],
        "苓雅區": ["前鎮區", "三民區"],
        "鹽埕區": ["鼓山區", "三民區"],
        "鼓山區": ["三民區", "左營區"],
        "旗津區": ["前鎮區"],
        "前鎮區": ["小港區", "鳳山區"],
        "三民區": ["左營區", "楠梓區", "鳥松區", "仁武區", "鳳山區"],
        "楠梓區": ["左營區", "仁武區", "橋頭區", "梓官區"],
        "小港區": ["鳳山區", "大寮區", "林園區"],
        "左營區": ["仁武區"],
        "仁武區": ["大社區", "鳥松區", "大樹區", "燕巢區", "橋頭區"],
        "大社區": ["燕巢區", "橋頭區"],
        "岡山區": ["橋頭區", "燕巢區", "田寮區", "阿蓮區", "路竹區", "彌陀區", "永安區"],
        "路竹區": ["阿蓮區", "湖內區", "茄萣區", "永安區"],
        "阿蓮區": ["湖內區", "田寮區"],
        "田寮區": ["燕巢區", "內門區", "旗山區"],
        "燕巢區": ["橋頭區", "旗山區", "大樹區"],
        "橋頭區": ["梓官區", "彌陀區"],
        "梓官區": ["彌陀區"],
        "彌陀區": ["永安區"],
        "永安區": ["茄萣區"],
        "湖內區": ["茄萣區"],
        "鳳山區": ["大寮區", "鳥松區", "仁武區"],
        "大寮區": ["林園區", "大樹區"],
        "鳥松區": ["大樹區"],
        "大樹區": ["旗山區"],
        "旗山區": ["內門區", "杉林區", "美濃區"],
        "美濃區": ["杉林區", "六龜區"],
        "六龜區": ["杉林區", "甲仙區", "桃源區", "茂林區"],
        "內門區": ["杉林區", "甲仙區"],
        "杉林區": ["甲仙區"],
        "甲仙區": ["那瑪夏區", "桃源區"],
        "桃源區": ["那瑪夏區", "茂林區"],
    },
    "屏東縣": {
        "屏東市": ["九如鄉", "長治鄉", "麟洛鄉", "萬丹鄉"],
        "三地門鄉": ["高樹鄉", "鹽埔鄉", "長治鄉", "霧台鄉", "瑪家鄉"],
        "霧台鄉": ["瑪家鄉"],
        "瑪家鄉": ["內埔鄉", "泰武鄉"],
        "九如鄉": ["里港鄉", "鹽埔鄉", "長治鄉"],
        "里港鄉": ["高樹鄉", "鹽埔鄉"],
        "高樹鄉": ["鹽埔鄉"],
        "鹽埔鄉": ["長治鄉", "內埔鄉"],
        "長治鄉": ["麟洛鄉", "內埔鄉"],
        "麟洛鄉": ["內埔鄉", "竹田鄉", "萬丹鄉"],
        "竹田鄉": ["內埔鄉", "萬丹鄉", "潮州鎮", "萬巒鄉"],
        "內埔鄉": ["萬巒鄉", "泰武鄉", "潮州鎮"],
        "萬丹鄉": ["新園鄉", "崁頂鄉"],
        "潮州鎮": ["萬巒鄉", "崁頂鄉", "新埤鄉", "南州鄉", "來義鄉"],
        "泰武鄉": ["萬巒鄉", "來義鄉"],
        "來義鄉": ["萬巒鄉", "新埤鄉", "春日鄉"],
        "崁頂鄉": ["新園鄉", "南州鄉", "東港鎮"],
        "新埤鄉": ["南州鄉", "春日鄉", "佳冬鄉"],
        "南州鄉": ["林邊鄉", "佳冬鄉"],
        "林邊鄉": ["佳冬鄉", "東港鎮"],
        "東港鎮": ["新園鄉"],
        "佳冬鄉": ["枋寮鄉"],
        "枋寮鄉": ["春日鄉", "枋山鄉"],
        "枋山鄉": ["春日鄉", "獅子鄉", "車城鄉"],
        "春日鄉": ["獅子鄉"],
        "獅子鄉": ["牡丹鄉", "車城鄉"],
        "車城鄉": ["牡丹鄉", "恆春鎮"],
        "牡丹鄉": ["恆春鎮", "滿州鄉"],
        "恆春鎮": ["滿州鄉"],
    },
    "宜蘭縣": {
        "宜蘭市": ["礁溪鄉", "壯圍鄉", "員山鄉", "五結鄉"],
        "頭城鎮": ["礁溪鄉", "員山鄉"],
        "礁溪鄉": ["壯圍鄉", "員山鄉"],
        "壯圍鄉": ["五結鄉"],
        "員山鄉": ["三星鄉", "大同鄉"],
        "羅東鎮": ["五結鄉", "冬山鄉", "三星鄉"],
        "三星鄉": ["冬山鄉", "大同鄉", "五結鄉"],
        "大同鄉": ["南澳鄉"],
        "五結鄉": ["冬山鄉", "蘇澳鎮"],
        "冬山鄉": ["蘇澳鎮"],
        "蘇澳鎮": ["南澳鄉"],
    },
    "花蓮縣": {
        "花蓮市": ["新城鄉", "吉安鄉", "秀林鄉"],
        "新城鄉": ["秀林鄉"],
        "秀林鄉": ["吉安鄉", "萬榮鄉"],
        "吉安鄉": ["壽豐鄉"],
        "壽豐鄉": ["鳳林鎮", "萬榮鄉", "豐濱鄉"],
        "鳳林鎮": ["萬榮鄉", "光復鄉"],
        "光復鄉": ["萬榮鄉", "瑞穗鄉", "豐濱鄉"],
        "豐濱鄉": ["瑞穗鄉"],
        "瑞穗鄉": ["萬榮鄉", "玉里鎮", "卓溪鄉"],
        "萬榮鄉": ["卓溪鄉"],
        "玉里鎮": ["卓溪鄉", "富里鄉"],
        "卓溪鄉": ["富里鄉"],
    },
    "台東縣": {
        "台東市": ["卑南鄉", "太麻里鄉"],
        "延平鄉": ["卑南鄉", "鹿野鄉", "海端鄉"],
        "卑南鄉": ["鹿野鄉", "太麻里鄉", "東河鄉", "金峰鄉"],
        "鹿野鄉": ["關山鎮", "東河鄉"],
        "關山鎮": ["海端鄉", "池上鄉", "東河鄉"],
        "海端鄉": ["池上鄉"],
        "池上鄉": ["長濱鄉"],
        "東河鄉": ["成功鎮"],
        "成功鎮": ["長濱鄉"],
        "太麻里鄉": ["金峰鄉", "大武鄉"],
        "金峰鄉": ["達仁鄉", "大武鄉"],
        "大武鄉": ["達仁鄉"],
    },
    "澎湖縣": {
        "馬公市": ["湖西鄉", "白沙鄉"],
        "白沙鄉": ["西嶼鄉"],
    },
    "金門縣": {
        "金沙鎮": ["金湖鎮", "金寧鄉"],
        "金湖鎮": ["金寧鄉", "金城鎮"],
        "金寧鄉": ["金城鎮"],
    },
    "連江縣": {},
}

# ---------------------------------------------------------------- cross-county
# Written once per pair, most-northern county first for readability.
CROSS: list[tuple[str, str]] = [
    # 台北市 / 新北市
    ("台北市/北投區", "新北市/淡水區"), ("台北市/北投區", "新北市/三芝區"),
    ("台北市/北投區", "新北市/金山區"), ("台北市/北投區", "新北市/萬里區"),
    ("台北市/士林區", "新北市/淡水區"), ("台北市/士林區", "新北市/萬里區"),
    ("台北市/士林區", "新北市/三重區"),
    ("台北市/大同區", "新北市/三重區"), ("台北市/中山區", "新北市/三重區"),
    ("台北市/萬華區", "新北市/板橋區"), ("台北市/萬華區", "新北市/中和區"),
    ("台北市/中正區", "新北市/永和區"), ("台北市/大安區", "新北市/永和區"),
    ("台北市/文山區", "新北市/永和區"), ("台北市/文山區", "新北市/中和區"),
    ("台北市/文山區", "新北市/新店區"), ("台北市/文山區", "新北市/深坑區"),
    ("台北市/文山區", "新北市/石碇區"),
    ("台北市/南港區", "新北市/深坑區"), ("台北市/南港區", "新北市/石碇區"),
    ("台北市/南港區", "新北市/汐止區"), ("台北市/內湖區", "新北市/汐止區"),
    # 基隆市 / 新北市
    ("基隆市/中正區", "新北市/瑞芳區"), ("基隆市/信義區", "新北市/瑞芳區"),
    ("基隆市/暖暖區", "新北市/瑞芳區"), ("基隆市/暖暖區", "新北市/平溪區"),
    ("基隆市/七堵區", "新北市/平溪區"), ("基隆市/七堵區", "新北市/汐止區"),
    ("基隆市/七堵區", "新北市/萬里區"), ("基隆市/安樂區", "新北市/萬里區"),
    ("基隆市/中山區", "新北市/萬里區"),
    # 新北市 / 桃園市
    ("新北市/林口區", "桃園市/龜山區"), ("新北市/林口區", "桃園市/蘆竹區"),
    ("新北市/新莊區", "桃園市/龜山區"), ("新北市/樹林區", "桃園市/龜山區"),
    ("新北市/鶯歌區", "桃園市/龜山區"), ("新北市/鶯歌區", "桃園市/八德區"),
    ("新北市/鶯歌區", "桃園市/大溪區"), ("新北市/三峽區", "桃園市/大溪區"),
    ("新北市/三峽區", "桃園市/復興區"), ("新北市/烏來區", "桃園市/復興區"),
    # 新北市 / 宜蘭縣
    ("新北市/坪林區", "宜蘭縣/頭城鎮"), ("新北市/坪林區", "宜蘭縣/礁溪鄉"),
    ("新北市/雙溪區", "宜蘭縣/頭城鎮"), ("新北市/貢寮區", "宜蘭縣/頭城鎮"),
    ("新北市/烏來區", "宜蘭縣/大同鄉"),
    # 桃園市 / 新竹縣
    ("桃園市/龍潭區", "新竹縣/關西鎮"), ("桃園市/龍潭區", "新竹縣/新埔鎮"),
    ("桃園市/楊梅區", "新竹縣/湖口鄉"), ("桃園市/楊梅區", "新竹縣/新埔鎮"),
    ("桃園市/新屋區", "新竹縣/湖口鄉"), ("桃園市/新屋區", "新竹縣/新豐鄉"),
    ("桃園市/復興區", "新竹縣/尖石鄉"), ("桃園市/復興區", "宜蘭縣/大同鄉"),
    # 新竹市 / 新竹縣
    ("新竹市/東區", "新竹縣/竹北市"), ("新竹市/北區", "新竹縣/竹北市"),
    ("新竹市/東區", "新竹縣/竹東鎮"), ("新竹市/東區", "新竹縣/寶山鄉"),
    ("新竹市/香山區", "新竹縣/寶山鄉"),
    # 新竹 / 苗栗
    ("新竹市/香山區", "苗栗縣/竹南鎮"), ("新竹縣/寶山鄉", "苗栗縣/頭份市"),
    ("新竹縣/峨眉鄉", "苗栗縣/頭份市"), ("新竹縣/峨眉鄉", "苗栗縣/三灣鄉"),
    ("新竹縣/五峰鄉", "苗栗縣/南庄鄉"), ("新竹縣/五峰鄉", "苗栗縣/泰安鄉"),
    ("新竹縣/尖石鄉", "台中市/和平區"), ("新竹縣/尖石鄉", "宜蘭縣/大同鄉"),
    # 苗栗 / 台中
    ("苗栗縣/苑裡鎮", "台中市/大甲區"), ("苗栗縣/苑裡鎮", "台中市/外埔區"),
    ("苗栗縣/三義鄉", "台中市/后里區"), ("苗栗縣/三義鄉", "台中市/外埔區"),
    ("苗栗縣/卓蘭鎮", "台中市/東勢區"), ("苗栗縣/卓蘭鎮", "台中市/石岡區"),
    ("苗栗縣/卓蘭鎮", "台中市/新社區"), ("苗栗縣/泰安鄉", "台中市/和平區"),
    # 台中 / 彰化 / 南投
    ("台中市/大肚區", "彰化縣/彰化市"), ("台中市/大肚區", "彰化縣/伸港鄉"),
    ("台中市/龍井區", "彰化縣/伸港鄉"), ("台中市/烏日區", "彰化縣/彰化市"),
    ("台中市/烏日區", "彰化縣/芬園鄉"), ("台中市/霧峰區", "彰化縣/芬園鄉"),
    ("台中市/霧峰區", "南投縣/草屯鎮"), ("台中市/太平區", "南投縣/國姓鄉"),
    ("台中市/新社區", "南投縣/國姓鄉"), ("台中市/東勢區", "南投縣/國姓鄉"),
    ("台中市/和平區", "南投縣/仁愛鄉"), ("台中市/和平區", "宜蘭縣/大同鄉"),
    ("台中市/和平區", "花蓮縣/秀林鄉"),
    # 彰化 / 南投 / 雲林
    ("彰化縣/芬園鄉", "南投縣/南投市"), ("彰化縣/芬園鄉", "南投縣/草屯鎮"), ("彰化縣/二水鄉", "南投縣/名間鄉"),
    ("彰化縣/二水鄉", "雲林縣/林內鄉"), ("彰化縣/溪州鄉", "雲林縣/西螺鎮"),
    ("彰化縣/竹塘鄉", "雲林縣/二崙鄉"), ("彰化縣/大城鄉", "雲林縣/麥寮鄉"),
    ("彰化縣/社頭鄉", "南投縣/名間鄉"),
    # 南投 / 雲林 / 嘉義 / 高雄 / 花蓮
    ("南投縣/竹山鎮", "雲林縣/林內鄉"), ("南投縣/竹山鎮", "雲林縣/古坑鄉"),
    ("南投縣/名間鄉", "雲林縣/林內鄉"), ("南投縣/竹山鎮", "嘉義縣/梅山鄉"),
    ("南投縣/信義鄉", "嘉義縣/阿里山鄉"), ("南投縣/信義鄉", "高雄市/桃源區"),
    ("南投縣/信義鄉", "花蓮縣/卓溪鄉"), ("南投縣/仁愛鄉", "花蓮縣/秀林鄉"),
    ("南投縣/仁愛鄉", "花蓮縣/萬榮鄉"),
    # 雲林 / 嘉義
    ("雲林縣/斗南鎮", "嘉義縣/大林鎮"), ("雲林縣/大埤鄉", "嘉義縣/大林鎮"),
    ("雲林縣/大埤鄉", "嘉義縣/溪口鄉"), ("雲林縣/古坑鄉", "嘉義縣/梅山鄉"),
    ("雲林縣/古坑鄉", "嘉義縣/大林鎮"), ("雲林縣/北港鎮", "嘉義縣/六腳鄉"),
    ("雲林縣/北港鎮", "嘉義縣/新港鄉"), ("雲林縣/元長鄉", "嘉義縣/新港鄉"),
    ("雲林縣/水林鄉", "嘉義縣/六腳鄉"), ("雲林縣/口湖鄉", "嘉義縣/東石鄉"),
    # 嘉義市 / 嘉義縣
    ("嘉義市/東區", "嘉義縣/番路鄉"), ("嘉義市/東區", "嘉義縣/竹崎鄉"),
    ("嘉義市/東區", "嘉義縣/中埔鄉"), ("嘉義市/東區", "嘉義縣/水上鄉"),
    ("嘉義市/西區", "嘉義縣/水上鄉"), ("嘉義市/西區", "嘉義縣/太保市"),
    ("嘉義市/西區", "嘉義縣/民雄鄉"), ("嘉義市/東區", "嘉義縣/民雄鄉"),
    # 嘉義 / 台南
    ("嘉義縣/義竹鄉", "台南市/鹽水區"), ("嘉義縣/義竹鄉", "台南市/學甲區"),
    ("嘉義縣/布袋鎮", "台南市/北門區"), ("嘉義縣/水上鄉", "台南市/後壁區"),
    ("嘉義縣/中埔鄉", "台南市/白河區"), ("嘉義縣/大埔鄉", "台南市/東山區"),
    ("嘉義縣/大埔鄉", "台南市/楠西區"), ("嘉義縣/大埔鄉", "台南市/南化區"),
    ("嘉義縣/阿里山鄉", "高雄市/那瑪夏區"), ("嘉義縣/阿里山鄉", "高雄市/桃源區"),
    # 台南 / 高雄
    ("台南市/仁德區", "高雄市/湖內區"), ("台南市/歸仁區", "高雄市/湖內區"),
    ("台南市/歸仁區", "高雄市/阿蓮區"), ("台南市/關廟區", "高雄市/阿蓮區"),
    ("台南市/關廟區", "高雄市/田寮區"), ("台南市/龍崎區", "高雄市/田寮區"),
    ("台南市/龍崎區", "高雄市/內門區"), ("台南市/南化區", "高雄市/內門區"),
    ("台南市/南化區", "高雄市/甲仙區"), ("台南市/南化區", "高雄市/杉林區"),
    # 高雄 / 屏東 / 台東
    ("高雄市/大寮區", "屏東縣/屏東市"), ("高雄市/大寮區", "屏東縣/萬丹鄉"),
    ("高雄市/大寮區", "屏東縣/新園鄉"), ("高雄市/林園區", "屏東縣/新園鄉"),
    ("高雄市/林園區", "屏東縣/東港鎮"), ("高雄市/大樹區", "屏東縣/九如鄉"),
    ("高雄市/大樹區", "屏東縣/里港鄉"), ("高雄市/旗山區", "屏東縣/里港鄉"),
    ("高雄市/美濃區", "屏東縣/里港鄉"), ("高雄市/美濃區", "屏東縣/高樹鄉"),
    ("高雄市/六龜區", "屏東縣/高樹鄉"), ("高雄市/六龜區", "屏東縣/三地門鄉"),
    ("高雄市/茂林區", "屏東縣/三地門鄉"), ("高雄市/茂林區", "屏東縣/霧台鄉"),
    ("高雄市/桃源區", "台東縣/海端鄉"), ("高雄市/桃源區", "花蓮縣/卓溪鄉"),
    ("屏東縣/牡丹鄉", "台東縣/達仁鄉"), ("屏東縣/獅子鄉", "台東縣/達仁鄉"),
    ("屏東縣/春日鄉", "台東縣/達仁鄉"), ("屏東縣/霧台鄉", "台東縣/延平鄉"),
    # 宜蘭 / 花蓮 / 台東
    ("宜蘭縣/南澳鄉", "花蓮縣/秀林鄉"), ("宜蘭縣/大同鄉", "花蓮縣/秀林鄉"),
    ("花蓮縣/富里鄉", "台東縣/池上鄉"), ("花蓮縣/富里鄉", "台東縣/長濱鄉"),
    ("花蓮縣/豐濱鄉", "台東縣/長濱鄉"), ("花蓮縣/卓溪鄉", "台東縣/海端鄉"),
]

# ------------------------------------------------------------------- commute
# Adjacency says two districts touch. It does not say what crossing the border
# costs, and for the geo graph that is the part that matters. Anything not
# listed here is treated as an ordinary boundary you drive across.
#
# `barrier` is what is in the way; `crossing` is what was built to defeat it;
# `commute` grades the result for a daily commuter.
BARRIERS: dict[tuple[str, str], dict[str, str]] = {
    ("新北市/八里區", "新北市/淡水區"): {
        "barrier": "river", "crossing": "淡江大橋 (2026-05-12)", "commute": "moderate",
        "note": "a 15km detour via 關渡大橋 until the bridge opened; the reported peak-hour saving is 25 minutes",
    },
    ("新北市/坪林區", "宜蘭縣/頭城鎮"): {
        "barrier": "mountain", "crossing": "雪山隧道 (國道5號)", "commute": "moderate",
        "note": "12.9km under the 雪山山脈; the alternative is the 北宜公路 mountain road",
    },
    ("新北市/坪林區", "宜蘭縣/礁溪鄉"): {
        "barrier": "mountain", "crossing": "北宜公路 台9線", "commute": "hard",
    },
    ("新北市/雙溪區", "宜蘭縣/頭城鎮"): {"barrier": "mountain", "commute": "hard"},
    ("新北市/貢寮區", "宜蘭縣/頭城鎮"): {
        "barrier": "coast", "crossing": "台2線 濱海公路", "commute": "moderate",
    },
    ("宜蘭縣/蘇澳鎮", "宜蘭縣/南澳鄉"): {
        "barrier": "mountain", "crossing": "蘇花改 台9線", "commute": "moderate",
    },
    ("宜蘭縣/南澳鄉", "花蓮縣/秀林鄉"): {
        "barrier": "mountain", "crossing": "蘇花改 觀音隧道", "commute": "hard",
        "note": "the 蘇花 corridor is the only land route between 宜蘭 and 花蓮",
    },
    ("屏東縣/牡丹鄉", "台東縣/達仁鄉"): {
        "barrier": "mountain", "crossing": "南迴改 台9線", "commute": "hard",
    },
    ("屏東縣/獅子鄉", "台東縣/達仁鄉"): {
        "barrier": "mountain", "crossing": "南迴改 草埔隧道", "commute": "hard",
    },
    ("屏東縣/春日鄉", "台東縣/達仁鄉"): {"barrier": "mountain", "commute": "impassable"},
    ("屏東縣/霧台鄉", "台東縣/延平鄉"): {"barrier": "mountain", "commute": "impassable"},
    # The Central Mountain Range. These borders exist on the map and no usable
    # road crosses them, which is exactly what a pure adjacency graph gets wrong.
    ("台中市/和平區", "花蓮縣/秀林鄉"): {
        "barrier": "mountain", "crossing": "中橫公路 台8線, partly closed since 1999",
        "commute": "impassable",
    },
    ("台中市/和平區", "宜蘭縣/大同鄉"): {
        "barrier": "mountain", "crossing": "台7甲線", "commute": "impassable",
    },
    ("台中市/和平區", "南投縣/仁愛鄉"): {"barrier": "mountain", "commute": "hard"},
    ("新竹縣/尖石鄉", "台中市/和平區"): {"barrier": "mountain", "commute": "impassable"},
    ("新竹縣/尖石鄉", "宜蘭縣/大同鄉"): {"barrier": "mountain", "commute": "impassable"},
    ("桃園市/復興區", "宜蘭縣/大同鄉"): {
        "barrier": "mountain", "crossing": "北橫公路 台7線", "commute": "hard",
    },
    ("桃園市/復興區", "新竹縣/尖石鄉"): {"barrier": "mountain", "commute": "impassable"},
    ("新北市/烏來區", "宜蘭縣/大同鄉"): {"barrier": "mountain", "commute": "impassable"},
    ("新北市/烏來區", "桃園市/復興區"): {"barrier": "mountain", "commute": "impassable"},
    ("南投縣/仁愛鄉", "花蓮縣/秀林鄉"): {
        "barrier": "mountain", "crossing": "中橫公路 台8線", "commute": "impassable",
    },
    ("南投縣/仁愛鄉", "花蓮縣/萬榮鄉"): {"barrier": "mountain", "commute": "impassable"},
    ("南投縣/信義鄉", "花蓮縣/卓溪鄉"): {"barrier": "mountain", "commute": "impassable"},
    ("南投縣/信義鄉", "高雄市/桃源區"): {"barrier": "mountain", "commute": "impassable"},
    ("南投縣/信義鄉", "嘉義縣/阿里山鄉"): {
        "barrier": "mountain", "crossing": "新中橫 台21線", "commute": "hard",
    },
    ("高雄市/桃源區", "台東縣/海端鄉"): {
        "barrier": "mountain", "crossing": "南橫公路 台20線", "commute": "hard",
    },
    ("高雄市/桃源區", "花蓮縣/卓溪鄉"): {"barrier": "mountain", "commute": "impassable"},
    ("嘉義縣/阿里山鄉", "高雄市/桃源區"): {"barrier": "mountain", "commute": "impassable"},
    ("嘉義縣/阿里山鄉", "高雄市/那瑪夏區"): {"barrier": "mountain", "commute": "impassable"},
    ("花蓮縣/卓溪鄉", "台東縣/海端鄉"): {"barrier": "mountain", "commute": "impassable"},
    # Rivers with enough bridges that the crossing is ordinary. Listed so the
    # behaviour graph is free to disagree about them.
    ("台北市/萬華區", "新北市/板橋區"): {
        "barrier": "river", "crossing": "華江橋、萬板大橋", "commute": "easy",
    },
    ("台北市/大同區", "新北市/三重區"): {
        "barrier": "river", "crossing": "台北橋、忠孝橋", "commute": "easy",
    },
    ("台北市/士林區", "新北市/三重區"): {
        "barrier": "river", "crossing": "重陽橋", "commute": "easy",
    },
    ("台北市/北投區", "新北市/淡水區"): {
        "barrier": "river", "crossing": "關渡大橋", "commute": "easy",
    },
    ("台北市/文山區", "新北市/新店區"): {"barrier": "river", "commute": "easy"},
    ("高雄市/林園區", "屏東縣/新園鄉"): {
        "barrier": "river", "crossing": "雙園大橋, 高屏溪", "commute": "moderate",
    },
    ("高雄市/大寮區", "屏東縣/屏東市"): {
        "barrier": "river", "crossing": "高屏大橋", "commute": "easy",
    },
    # Mountain borders that sit inside a single county.
    ("台北市/北投區", "新北市/萬里區"): {"barrier": "mountain", "commute": "hard"},
    ("台北市/士林區", "新北市/萬里區"): {"barrier": "mountain", "commute": "hard"},
    ("台北市/北投區", "新北市/金山區"): {"barrier": "mountain", "commute": "hard"},
    ("新北市/三峽區", "新北市/烏來區"): {"barrier": "mountain", "commute": "impassable"},
    ("新北市/新店區", "新北市/坪林區"): {
        "barrier": "mountain", "crossing": "北宜公路", "commute": "moderate",
    },
    ("台中市/東勢區", "台中市/和平區"): {
        "barrier": "mountain", "crossing": "台8線", "commute": "moderate",
    },
    ("南投縣/埔里鎮", "南投縣/仁愛鄉"): {
        "barrier": "mountain", "crossing": "台14線", "commute": "moderate",
    },
}

# Islands have no land border. Recording the link they do have keeps them from
# looking like isolated nodes when they are simply reached another way.
ISLAND_LINKS: list[dict[str, object]] = [
    {"district": "屏東縣/琉球鄉", "mainland": "屏東縣/東港鎮", "link": "ferry", "commute": "hard"},
    {"district": "台東縣/綠島鄉", "mainland": "台東縣/台東市", "link": "ferry/air", "commute": "impassable"},
    {"district": "台東縣/蘭嶼鄉", "mainland": "台東縣/台東市", "link": "ferry/air", "commute": "impassable"},
    {"district": "澎湖縣/望安鄉", "mainland": "澎湖縣/馬公市", "link": "ferry", "commute": "hard"},
    {"district": "澎湖縣/七美鄉", "mainland": "澎湖縣/馬公市", "link": "ferry/air", "commute": "hard"},
    {"district": "金門縣/烈嶼鄉", "mainland": "金門縣/金城鎮", "link": "金門大橋", "commute": "moderate"},
    {"district": "金門縣/烏坵鄉", "mainland": None, "link": "none", "commute": "impassable"},
    {"district": "連江縣/南竿鄉", "mainland": None, "link": "ferry/air", "commute": "impassable"},
    {"district": "連江縣/北竿鄉", "mainland": "連江縣/南竿鄉", "link": "ferry", "commute": "hard"},
    {"district": "連江縣/莒光鄉", "mainland": "連江縣/南竿鄉", "link": "ferry", "commute": "impassable"},
    {"district": "連江縣/東引鄉", "mainland": "連江縣/南竿鄉", "link": "ferry", "commute": "impassable"},
]

COMMUTE_ORDER = ["easy", "moderate", "hard", "impassable"]


def build(l4_path: Path) -> dict:
    known = {
        f"{row['county']}/{row['district']}"
        for row in json.loads(l4_path.read_text(encoding="utf-8"))["districts"]
    }
    pairs: dict[frozenset[str], dict[str, object]] = {}
    unknown: list[str] = []

    def add(a: str, b: str, scope: str) -> None:
        for node in (a, b):
            if node not in known:
                unknown.append(node)
                return
        if a != b:
            pairs.setdefault(frozenset((a, b)), {"scope": scope})

    for county, table in INTRA.items():
        for district, neighbours in table.items():
            for neighbour in neighbours:
                add(f"{county}/{district}", f"{county}/{neighbour}", "intra_county")
    for a, b in CROSS:
        add(a, b, "cross_county")

    for (a, b), attrs in BARRIERS.items():
        key = frozenset((a, b))
        if key not in pairs:
            unknown.append(f"barrier on non-adjacent pair {a}--{b}")
            continue
        pairs[key].update(attrs)

    edges = [
        {
            "a": sorted(key)[0],
            "b": sorted(key)[1],
            "scope": attrs.get("scope"),
            "barrier": attrs.get("barrier", "none"),
            "crossing": attrs.get("crossing"),
            "commute": attrs.get("commute", "easy"),
            "note": attrs.get("note"),
        }
        for key, attrs in pairs.items()
    ]
    edges.sort(key=lambda edge: (edge["a"], edge["b"]))

    degree: dict[str, int] = {node: 0 for node in known}
    for edge in edges:
        degree[edge["a"]] += 1
        degree[edge["b"]] += 1
    islands = {row["district"] for row in ISLAND_LINKS}
    stranded = sorted(
        node for node, count in degree.items() if count == 0 and node not in islands
    )

    return {
        "schema": "skillweave-geo-adjacency-v1",
        "provenance": "authored",
        "method": "hand-authored land-border adjacency between the 368 official districts",
        "honesty": (
            "the least trustworthy artifact in the repo, because it is written "
            "from geographic knowledge rather than measured. "
            "scripts/validate_geo_adjacency.py scores it against the behaviour "
            "graph and app/geo_graph.py loads it as a separately ablatable layer"
        ),
        "commute_scale": {
            "easy": "an ordinary boundary, crossed without noticing",
            "moderate": "a specific bridge, tunnel or pass carries the traffic",
            "hard": "a mountain road or a ferry; not a daily commute for most",
            "impassable": "the border exists on the map and no usable road crosses it",
        },
        "counts": {
            "districts": len(known),
            "edges": len(edges),
            "intra_county": sum(1 for e in edges if e["scope"] == "intra_county"),
            "cross_county": sum(1 for e in edges if e["scope"] == "cross_county"),
            "by_commute": {
                grade: sum(1 for e in edges if e["commute"] == grade)
                for grade in COMMUTE_ORDER
            },
            "by_barrier": {
                barrier: sum(1 for e in edges if e["barrier"] == barrier)
                for barrier in sorted({e["barrier"] for e in edges})
            },
            "islands": len(ISLAND_LINKS),
            "districts_with_no_land_neighbour": len(stranded),
        },
        "unknown_nodes": sorted(set(unknown)),
        "districts_with_no_land_neighbour": stranded,
        "island_links": ISLAND_LINKS,
        "edges": edges,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l4", type=Path, default=DEFAULT_L4)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build(args.l4)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["counts"], ensure_ascii=False, indent=2))
    if payload["unknown_nodes"]:
        print("unknown:", json.dumps(payload["unknown_nodes"], ensure_ascii=False, indent=1))
    if payload["districts_with_no_land_neighbour"]:
        print("no land neighbour:", json.dumps(payload["districts_with_no_land_neighbour"], ensure_ascii=False))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
