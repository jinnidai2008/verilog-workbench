# project.json 配置契约

> 这一层只回答一个问题：**「探测哪个工程、工程里各叫什么」**。
> 它不执行任何 EDA 命令 —— 那是 `refresh.py` 的活。

## 为什么需要它

改造前 `refresh.py` 里写死了这些东西：

| 写死的内容 | 位置 |
|---|---|
| 工程目录 `APP_DIR.parent / "vibe-verilog-lab"` | 第 29 行 |
| WSL 发行版 `"Ubuntu-24.04"` | 第 32 行 |
| 命令超时 `240` | 第 51 行 |
| `counter.v` / `tb_counter.v` | 第 78、80、96、103、121 行 |
| 顶层模块 `counter` | 第 120、121 行 |
| 波形 `counter.vcd` | 第 145、146、150、163 行 |

结果：**只能探测那一个例子**。加个新工程就得改代码 —— 而"能对任意项目跑探测"是后面所有步骤的地基。

## 文件位置

```
desk-app/                      ← 通用工具（探测逻辑在这里，一份就够）
  refresh.py
  project_config.py            ← 本契约的校验器
  project.example.json         ← 模板

vibe-verilog-lab/              ← 被探测的工程 A
  project.json                 ← 工程自己的配置
  counter.v  tb_counter.v

adder-lab/                     ← 以后的新工程 B，与 A 平级
  project.json
  adder.v    tb_adder.v
```

**约定**：`project.json` 放在**工程根目录**，路径一律相对**它自己所在的目录**解析。
探测器从哪里被调用、当前工作目录是什么，都不影响解析结果。

## 字段定义

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `version` | 整数 | 否 | `1` | 配置格式版本。目前只支持 `1`，留作以后不兼容升级的判据 |
| `name` | 字符串 | **是** | — | 项目名。显示在界面上，也用来在 `board.json` 里标识是哪个项目 |
| `top` | 字符串 | **是** | — | 综合的顶层模块名，传给 `yosys -top <top>`。**是 Verilog 里 `module` 后面的名字，不是文件名** |
| `dut` | 字符串 | **是** | — | 被测设计源文件，相对工程目录。**文件必须真实存在** |
| `tb` | 字符串 | **是** | — | 测试平台文件，相对工程目录。**文件必须真实存在** |
| `vcd` | 字符串 | **是** | — | 波形文件名。**必须和测试平台里 `$dumpfile("...")` 写的一致** |
| `assertPattern` | 字符串 | 否 | `\b\w+\s*=\s*(\d+)` | 从仿真输出里抓检查点的正则（**第一个捕获组**是要取的数字）。默认抓 `名字 = 数字` 这种打印 |
| `timeout` | 整数 | 否 | `240` | 每条 EDA 命令的超时（秒）。范围 10–3600 |
| `distro` | 字符串 | 否 | `Ubuntu-24.04` | 跑工具链的 WSL 发行版名 |

### 最小可用配置

只有 5 个字段是必需的：

```json
{
  "name": "adder",
  "top": "adder",
  "dut": "adder.v",
  "tb": "tb_adder.v",
  "vcd": "adder.vcd"
}
```

`version` / `timeout` / `distro` 全部走默认值。

## 三条设计决策

### ① 只配置「输入」，不配置「生成物」

`sim.vvp`、`sim.log` 这类**脚本自己产出的中间文件不进配置**，固定命名即可。
否则配置项会无限膨胀，而它们本来就不需要用户操心。

只有**用户才知道答案**的东西才进配置 —— 比如波形文件名，因为那是 testbench 的
`$dumpfile()` 决定的，脚本无从推断。

### ② 未知字段只警告，不报错

```json
{ "name": "a", "futureField": 1, "_备注": "写给自己看的" }
```

- `_` 开头的字段：**静默忽略**（当作注释用，JSON 本身不支持注释）
- 其他未知字段：**警告**，不影响使用

这样以后加字段不会让老配置失效，用户也能在配置里写注释。

### ③ 编码容忍 BOM

Windows 上记事本、PowerShell 的 `Set-Content -Encoding UTF8` 都会给文件加 BOM。
按纯 `utf-8` 读会得到一句话都看不懂的 `Unexpected UTF-8 BOM`。

**所以读取一律用 `utf-8-sig`** —— 带不带 BOM 都能读。
这是实测踩出来的坑，已固化成测试用例 ⓪。

## 校验规则

校验在**跑任何 EDA 命令之前**完成，且**一次报全所有问题**（不是发现一个就退出）。

| 检查 | 错误码 | 通过条件 |
|---|---|---|
| 配置文件存在 | `config/missing` | 目录里有 `project.json` |
| JSON 合法 | `config/bad-json` | 能被解析（带 BOM 也行） |
| 最外层是对象 | `config/not-object` | 是 `{ }` 而不是数组或字符串 |
| 版本兼容 | `version/type` `version/unsupported` | `version` 是整数且等于 1 |
| 必填字段 | `field/missing:<字段>` | `name` `top` `dut` `tb` `vcd` 都在 |
| 类型正确 | `field/type:<字段>` | 字符串字段是字符串，`timeout` 是整数 |
| 非空 | `field/empty:<字段>` | 不是空白字符串 |
| 取值范围 | `field/range:<字段>` | `timeout` 在 10–3600 |
| 文件存在 | `file/not-found:<字段>` | `dut` `tb` 指向的文件真的在 |
| 正则可编译 | `field/bad-regex:assertPattern` | `assertPattern` 能被 `re.compile` |
| 交叉一致 | `cross/dut-eq-tb` | `dut` 和 `tb` 不是同一个文件 |
| 未知字段 | `field/unknown` | **仅警告** |

`timeout` 的类型检查特别排除了布尔值 —— Python 里 `True` 是 `int` 的子类，
不排除的话 `"timeout": true` 会被当成合法数字。

## 错误信息的样子

契约的价值一半在**报错说不说得清**。实际输出：

```
校验配置：C:\...\_pcfg\project.json

  [错误] 缺少必填字段 "top"：综合的顶层模块名，对应 yosys 的 synth -top <top>。
         必须是 Verilog 里 module 后面的名字，不是文件名。
         建议：在 project.json 里加上 "top": ...
  [错误] "tb" 指向的文件不存在：tb_addr.v（完整路径 C:\...\_pcfg\tb_addr.v）
         建议：工程目录里现有的 .v 文件：adder.v、tb_adder.v
  [错误] "timeout" 应为整数，实际是字符串（'240'）。
         建议：每条 EDA 命令的超时（秒）。大工程综合慢就调大。

未通过：3 个错误，0 个提醒。
```

三个要点：
- **一次报全**，不用改一个跑一次
- **说清期望什么、实际是什么**（`应为整数，实际是字符串（'240'）`）
- **给出下一步动作**，文件名打错时直接列出目录里现有的 `.v` 文件

## 怎么用

```bash
# 人话报告
python project_config.py D:\steam\Documents\dsh\vibe-verilog-lab

# 机器可读（给界面/脚本调）
python project_config.py <工程目录> --json

# 契约回归测试（13 个用例）
python test_project_config.py
```

退出码：`0` 通过 / `1` 配置有问题 / `2` 用法错误。

`--json` 的输出结构：

```json
{
  "projectDir": "D:\\...\\vibe-verilog-lab",
  "ok": true,
  "config": { "version": 1, "name": "4bit-counter", "top": "counter", ... },
  "errors": [],
  "warnings": []
}
```

`config` 里是**合并默认值之后**的完整结果 —— 后面的模块直接用它，不用自己再填默认值。

## 给后续模块的接口

第 2 部分（项目解析层）只需要调这两个函数：

```python
from project_config import load_config, validate

raw, issues = load_config(project_dir)      # 读文件（文件缺失也算 issue）
resolved, more = validate(raw, project_dir)  # 校验 + 合并默认值
issues += more
if any(i.level == "error" for i in issues):
    ...  # 拦下来，不要把残缺配置传给 EDA 命令
```

## 工程目录怎么确定

`refresh.py` 按下面的顺序找工程，**先命中先用**：

| 顺序 | 来源 | 用法 |
|---|---|---|
| 1 | **命令行参数** | `python refresh.py D:\...\adder-lab` |
| 2 | **环境变量** | `set DSH_VERILOG_PROJECT=D:\...\adder-lab` |
| 3 | **指路牌文件** | `desk-app/project.json` 里的 `"projectDir": "..\\adder-lab"` |
| 4 | **默认同级目录** | `desk-app` 旁边的 `vibe-verilog-lab` |

第 3 项是给界面准备的 —— 服务端（`server.py`）用同一套顺序，
所以**改指路牌就能切换界面探测哪个工程**，不用碰代码。

`server.py` 在真正跑 EDA 之前会先做一次同样的校验（预检），
**配置有问题就直接告诉界面，一条 EDA 命令都不会执行**。

## 一个必须知道的坑：wsl.exe 不转发退出码

实测结果（**这是判断成败时最容易踩的坑**）：

| 情况 | Linux 里真实的退出码 | `wsl.exe` 返回的 |
|---|---|---|
| 命令正常 | 0 | 0 |
| `exit 7` | 7 | **7**（会转发） |
| 命令不存在 | 127 | **127**（会转发） |
| 发行版不存在 | — | **-1**（会转发） |
| **verilator 报语法错误** | 1 | **0** ← **不转发！** |
| **verilator 有警告并退出** | 1 | **0** ← **不转发！** |

**所以不能靠 `returncode` 判断 EDA 命令成没成** —— 只能看输出文本。
`refresh.py` 里的 `classify()` 就是干这个的：

- 有真正的 `%Error` 行（**排除** `%Error: Exiting due to N warning(s)` 这种收尾语）→ `fail`
- 出现 `command not found` / `WSL_E_DISTRO` → `fail`（工具链没装好）
- 有 `%Warning` 但没有真错误 → `warn`
- 都没有 → `ok`

> 写这段时踩过一个坑：一开始用负向前瞻 `(?!Exiting due to)` 塞进同一个正则，
> 结果 `\s*` 会回溯到零宽、前瞻失效，警告被误判成错误。
> 现在拆成「先找 `^%Error` 行、再逐行排除收尾语」两步，直白且可测。

## 现状

- ✅ 契约定义完成，13/13 测试通过
- ✅ `refresh.py` 已通用化 —— 8 处硬编码全部改为读配置
- ✅ `server.py` 已接上，含预检、失败原因回传、预检结果缓存
- ✅ 新工程验收通过：`adder-lab` 五步全绿
- ✅ 老工程无回归：除时间戳外与改造前逐字段一致
- ✅ 状态判定已修正：警告 → `warn`（黄），真错误 → `fail`（红）
- ✅ 界面步骤条补齐 `warn` / `fail` 配色
- ✅ 全部测试 **48 项通过**（契约 13 + 验收 24 + 边界 11）
- ⏸ 界面未接入项目选择器 —— 按约定不碰

### 已知的糙（留给后续步骤）

| 项 | 影响 | 计划 |
|---|---|---|
| 检查点默认按 `名字 = 数字` 抓 | 换一种打印格式的 testbench 会数错 | 已开 `assertPattern` 兜底，默认值仍是猜的；第 4 步做错误分类时一起收拾 |

### 关于「和改造前完全一致」

`vibe-verilog-lab/project.json` 的值等价于改造前写死在代码里的那套，
输出**逐字段一致**，唯一差异是 `sim.cmd`：
改造前漏写了实际在用的 `-Wall`，现在和真实命令一致了。
