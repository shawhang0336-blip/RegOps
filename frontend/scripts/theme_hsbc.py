"""把绿色系主题整体换成汇丰红。

做三件事：
1) 重写两个 :root 调色板（亮色在 base.css，暗色覆盖在 styles.css），
   并把"品牌色/交互色"从 --green 拆成独立的 --brand，成功语义继续用 --green；
2) 把散落的硬编码绿色 hex / rgba 映射成中性灰或品牌红；
3) 侧栏、品牌方块、进度步骤这些局部样式单独改。

运行： python scripts/theme_hsbc.py [--dry]
"""

import pathlib
import re
import sys

FRONTEND = pathlib.Path(__file__).resolve().parents[1]
FILES = ["src/base.css", "src/pages.css", "src/styles.css"]

LIGHT_ROOT = """:root {
  font-family: Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif;
  color: #1c1c1c;
  background: #f7f6f5;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;

  /* 中性色：偏暖的灰，和汇丰红同属暖色系，避免发绿 */
  --ink: #1c1c1c;
  --ink-secondary: #3d3d3d;
  --muted: #6b6b6b;
  --muted-light: #9b9b9b;
  --surface: #ffffff;
  --surface-subtle: #faf9f8;
  --surface-hover: #f2f0ef;
  --line: #e3dfdd;
  --line-subtle: #eeecea;

  /* 品牌色（汇丰红）：按钮、链接、选中态、焦点环 */
  --brand: #db0011;
  --brand-hover: #b3000e;
  --brand-light: #fdeced;
  --brand-border: #f3b9bd;

  /* 语义色：成功仍然用绿，和品牌红区分开 */
  --green: #15734a;
  --green-hover: #0f583a;
  --green-light: #e8f3ed;
  --green-border: #b6d8c7;

  --amber: #8a5a0a;
  --amber-light: #fdf4e3;
  --amber-border: #ecd3a0;

  --red: #b0000f;
  --red-light: #fbeaea;
  --red-border: #f0bfc3;

  --blue: #1f4e79;
  --blue-light: #eaf1f8;

  --violet: #5b3fa8;
  --violet-light: #f0ecfb;

  --navy: #171717;
  --navy-elevated: #272727;
  --navy-border: #323232;
  --radius-sm: 7px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --shadow-sm: 0 1px 3px rgba(28, 28, 28, 0.06), 0 1px 2px rgba(28, 28, 28, 0.04);
  --shadow-md: 0 8px 24px rgba(28, 28, 28, 0.1);
}"""

DARK_ROOT = """:root[data-theme="dark"] {
  color-scheme: dark;
  color: #f2f0ef;
  background: #111110;

  --ink: #f2f0ef;
  --ink-secondary: #cfcac7;
  --muted: #a09a97;
  --muted-light: #7a7471;
  --surface: #1a1918;
  --surface-subtle: #211f1e;
  --surface-hover: #292624;
  --line: #3a3634;
  --line-subtle: #2b2928;

  --brand: #ff5a63;
  --brand-hover: #ff7a82;
  --brand-light: #3a1518;
  --brand-border: #6b252b;

  --green: #3fbf86;
  --green-hover: #4fd295;
  --green-light: #12291f;
  --green-border: #225c43;

  --amber: #eab35a;
  --amber-light: #2f2413;
  --amber-border: #5e4a24;

  --red: #ff6b5e;
  --red-light: #331614;
  --red-border: #6b2a24;

  --blue: #74b4f5;
  --blue-light: #142433;

  --violet: #b49bfa;
  --violet-light: #241d3a;

  --navy: #0b0b0b;
  --navy-elevated: #1c1c1c;
  --navy-border: #2c2c2c;
  --shadow-sm: 0 2px 6px rgba(0, 0, 0, 0.34);
  --shadow-md: 0 12px 32px rgba(0, 0, 0, 0.56);
}"""

# 交互/品牌语义 → 换成 --brand 系列（成功语义继续用 --green）
BRAND_LINES = {
    "src/base.css": {89, 316, 359, 365, 402, 410},
    "src/pages.css": {35, 36, 61, 83, 133, 138, 150, 228, 229, 230, 241, 290, 370, 648, 696, 708,
                      781, 782, 801, 955, 956, 980, 981, 1012},
    "src/styles.css": {68, 69, 80, 99, 110, 155, 200, 491, 492, 493, 574, 649},
}

HEX_MAP = {
    # ---- 暗色面板底色（原来是绿黑）→ 中性黑 ----
    "#10241e": "#171717", "#19352c": "#272727", "#26493e": "#323232", "#11231d": "#151414",
    "#0e2c22": "#1a1a1a", "#0e2e23": "#1a1a1a", "#366857": "#3f3f3f", "#08110e": "#0b0b0b",
    "#13261f": "#1c1c1c", "#1e382f": "#2b2b2b", "#12211c": "#1a1918", "#172a23": "#211f1e",
    "#1d352c": "#292624", "#253d34": "#3a3634", "#1c3028": "#2b2928", "#101f19": "#161514",
    "#10231c": "#181716", "#10261d": "#191817", "#143326": "#221f1d", "#152720": "#1e1c1b",
    "#152b23": "#201e1d", "#153025": "#221f1e", "#162922": "#1f1d1c", "#173328": "#232120",
    "#17362a": "#242221", "#182c24": "#201e1d", "#183329": "#232120", "#192e26": "#211f1e",
    "#1a3028": "#22201f", "#284c3d": "#333130", "#294a3d": "#32302f", "#295c46": "#3a3837",
    "#2b694f": "#41403e", "#2d634d": "#3e3c3b", "#366953": "#464443",
    "#13251f": "#1c1b1a", "#142620": "#1e1c1b",
    # ---- 文字/边框的灰绿 → 中性灰 ----
    "#8aa196": "#a09a97", "#657c71": "#7a7471", "#9cb5ab": "#b0aaa7", "#79968a": "#8f8986",
    "#b4c8bf": "#cfcac7", "#ecf8f2": "#f2f0ef", "#a9c4b8": "#c4bfbc", "#84a094": "#9a9491",
    "#b5d8c8": "#d8d3d0", "#8ca69b": "#a29c99", "#bdd0c6": "#cfcac7", "#e6f0eb": "#f2f0ef",
    "#cfe1d4": "#dcd7d4", "#8bc4a6": "#b6b0ad", "#9ec5b0": "#b8b2af", "#b5dec7": "#d8d3d0",
    "#b7c8be": "#c9c4c1", "#4f5d54": "#5a5654", "#204233": "#2b2a29", "#37473f": "#3d3d3d",
    "#5e6d64": "#6b6b6b", "#829188": "#9b9b9b", "#14211b": "#1c1c1c", "#d9e2dc": "#e3dfdd",
    "#d5e2da": "#e3dfdd", "#2c4c3e": "#3d3d3d",
    # ---- 品牌绿 → 汇丰红 ----
    "#146c4e": "#db0011", "#0e543c": "#b3000e", "#e3f4ec": "#fdeced", "#a3d9bf": "#f3b9bd",
    "#e8f5ee": "#fdeced", "#e7f6ed": "#fdeced", "#eef8f2": "#fdf3f3", "#edf8f2": "#fdf3f3",
    "#b8f5d2": "#f0626b", "#68d39a": "#db0011", "#89e4b5": "#ffffff", "#e6f6ed": "#fdeced",
    "#9ad4b6": "#f3b9bd", "#eec5b0": "#f3b9bd",
    # ---- 成功语义的绿保留，但换成本主题的绿 ----
    "#68e3a3": "#3fbf86",
    # ---- 代码块/浅底 ----
    "#def3e8": "#f0eeec", "#dcf2e6": "#f0eeec", "#d8eee1": "#ececea", "#9be5c4": "#f0eeec",
    "#a7d8c2": "#cfcac7", "#a5f2ce": "#ffb3b8", "#7cc4a3": "#ff9aa1", "#eef3ef": "#f0eeec",
    "#eef4f0": "#f2f0ef", "#edf1ee": "#f0eeec", "#f9fbf9": "#faf9f8", "#ecf7f1": "#f7f6f5",
    "#f3f6f4": "#f7f6f5",
}

RGBA_MAP = {
    "rgba(16, 36, 30, 0.05)": "rgba(28, 28, 28, 0.06)",
    "rgba(16, 36, 30, 0.03)": "rgba(28, 28, 28, 0.04)",
    "rgba(16, 36, 30, 0.08)": "rgba(28, 28, 28, 0.1)",
    "rgba(16, 36, 30, 0.55)": "rgba(17, 17, 16, 0.58)",
    "rgba(20, 108, 78, 0.12)": "rgba(219, 0, 17, 0.12)",
    "rgba(20, 108, 78, 0.2)": "rgba(219, 0, 17, 0.2)",
    "rgba(20, 108, 78, 0.08)": "rgba(219, 0, 17, 0.08)",
    "rgba(20, 108, 78, 0.22)": "rgba(219, 0, 17, 0.22)",
    "rgba(38, 166, 120, 0.14)": "rgba(219, 0, 17, 0.14)",
    "rgba(104, 227, 163, 0.2)": "rgba(63, 191, 134, 0.22)",
}

SPECIAL = [
    # 品牌方块：汇丰红实底 + 白字
    (
        """  background: linear-gradient(135deg, #f0626b 0%, #db0011 100%);
  color: #1a1a1a;""",
        """  background: linear-gradient(135deg, #e4001a 0%, #b3000e 100%);
  color: #ffffff;""",
    ),
    # 选中导航小方块：红底白字
    (
        """.nav-item.active .nav-step-badge {
  background: #ffffff;
  color: #1a1a1a;
}""",
        """.nav-item.active .nav-step-badge {
  background: var(--brand);
  color: #ffffff;
}""",
    ),
    # 选中导航项：深灰底 + 左侧红条
    (
        """.nav-item.active {
  background: var(--navy-elevated);
  border-color: #3f3f3f;
  color: #ffffff;""",
        """.nav-item.active {
  background: var(--navy-elevated);
  border-color: #3f3f3f;
  border-left: 3px solid var(--brand);
  color: #ffffff;""",
    ),
]

BRAND_TOP_RULE = """
/* 汇丰红顶条：全站品牌识别 */
body::before {
  content: "";
  position: fixed;
  inset: 0 0 auto 0;
  height: 3px;
  background: var(--brand);
  z-index: 60;
}
"""


def apply() -> None:
    dry = "--dry" in sys.argv
    for rel in FILES:
        path = FRONTEND / rel
        text = path.read_text()
        original = text

        # 1) 交互/品牌语义换成 --brand（必须先做：此时行号还是文件原始行号）
        lines = text.split("\n")
        for index in sorted(BRAND_LINES.get(rel, set())):
            line = lines[index - 1]
            replaced = (
                line.replace("--green-light", "--brand-light")
                .replace("--green-border", "--brand-border")
                .replace("--green-hover", "--brand-hover")
                .replace("--green", "--brand")
            )
            if replaced != line:
                lines[index - 1] = replaced
            else:
                print(f"  [warn] {rel} 第 {index} 行没有 --green：{line.strip()[:60]}")
        text = "\n".join(lines)

        # 2) 重写调色板
        if rel == "src/base.css":
            start = text.index(":root {")
            end = text.index("\n}", text.index("--shadow-md:", start)) + 2
            text = text[:start] + LIGHT_ROOT + text[end:]
        if rel == "src/styles.css":
            start = text.index(':root[data-theme="dark"] {')
            end = text.index("\n}", text.index("--shadow-md:", start)) + 2
            text = text[:start] + DARK_ROOT + text[end:]

        # 3) 硬编码绿 → 中性色 / 品牌红
        for old, new in RGBA_MAP.items():
            text = text.replace(old, new)
        for old, new in HEX_MAP.items():
            text = re.sub(re.escape(old) + r"\b", new, text, flags=re.I)

        # 4) 局部样式
        if rel == "src/base.css":
            for old, new in SPECIAL:
                if old in text:
                    text = text.replace(old, new)
                else:
                    print(f"  [skip] 未匹配的特殊规则：{old.splitlines()[0][:50]}")
            if "汇丰红顶条" not in text:
                text = text.rstrip() + "\n" + BRAND_TOP_RULE

        print(f"{rel}: {len(original)} -> {len(text)} chars")
        if not dry:
            path.write_text(text)


if __name__ == "__main__":
    apply()
