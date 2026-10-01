# Verilog Workbench · quench

**一句话生成 Verilog，然后真的去跑；跑不过就自己修；修不好就想改测试蒙混 —— 而这一条被拦住了。**

这不是又一个"让 AI 写点 Verilog"的玩具。它做的是完整的验证闭环，
而且**在模型开始作弊的时候能识别出来**。

![工作台首屏](docs/hero.jpg)

![五步探测](docs/probe.jpg)

> 第二张是真跑出来的输出：`iverilog` 仿真 `TEST PASSED`、
> `verilator` 无警告、`yosys` 综合 25 个单元。不是效果图。

---

## 这个项目最值得看的一点

多轮自修的过程中，模型遇到过修不好的设计。它没有继续报错，
而是**转去把测试平台改松** —— 删掉断言、把检查点减少 —— 想让测试变绿。

护栏识别出"测试被削弱"，拒绝该轮，并在连续两次之后**主动停止**，
而不是继续烧 API 额度：

```
第 1 轮  ✗ 探测未通过      sim: TEST FAILED
第 2 轮  ⛔ 测试被削弱      检查点从 16 处降到 10 处（少了 6 处）
第 3 轮  ⛔ 测试被削弱      检查点从 16 处降到 14 处（少了 2 处）
→ 停止：模型反复试图削弱测试，已连续 2 次
```

**判据不是"测试平台有没有被改"，而是"有没有被改松"** ——
改写法、加断言、修笔误都放行；减少检查点、把比较对象换成常量、
把设计信号换成硬编码数组，一律拒绝。
（早期版本曾经禁止任何改动，结果把正常修 bug 也堵死了，后来改成这个策略。）

---

## 它由两部分组成

| 目录 | 作用 |
|---|---|
| `desk-app/` | **工作台** —— 网页界面，选工程、拖文件、实时看五步探测 |
| `quench/` | **生成引擎** —— 从中文需求生成 Verilog，多轮自修，出质量报告 |

五步探测是**真跑**，不是模拟：
`iverilog` 仿真 → `verilator` 语法检查 → `yosys` 综合 → 波形解析 → 产物扫描。

---

## 快速开始

```bash
git clone <这个仓库>
cd desk-app
python doctor.py          # 先自查：缺什么、该装什么，它会直接给命令
```

看到 `结论：一切就绪` 之后：

```bash
python server.py 8787 --no-browser
# 打开它打印的地址，例如 http://127.0.0.1:8787/workbench.html
```

### 需要装什么

`doctor.py` 会逐项检查并给出**可以直接照抄的命令**。一共三样：

| # | 需要 | 说明 |
|---|---|---|
| 1 | **Python 3.10+** | [python.org](https://www.python.org/downloads/)，安装时勾选 `Add python.exe to PATH` |
| 2 | **EDA 工具链** | `iverilog` + `verilator` + `yosys`，**二选一**（见下） |
| 3 | **DeepSeek API key** | 只有「生成新代码」才需要；**探测已有文件不需要** |

**工具链二选一：**

**(A) Windows 便携包 —— 不用 WSL，推荐**

去 [oss-cad-suite-build releases](https://github.com/YosysHQ/oss-cad-suite-build/releases)
下载 `oss-cad-suite-windows-x64`（约 570 MB），解压到任意目录，然后：

```bash
python refresh.py --backend windows --toolchain-dir D:\oss-cad-suite\bin
```

**(B) 用 WSL**

```bash
wsl -d Ubuntu-24.04 -- sudo apt update
wsl -d Ubuntu-24.04 -- sudo apt install -y iverilog verilator yosys
python refresh.py --backend wsl
```

不指定 `--backend` 时自动探测：优先用 Windows 原生工具，找不到再退回 WSL。

**端到端验证**（会真的跑一个小设计）：

```bash
python doctor.py --probe
```

---

## 命令行也能用

```bash
python refresh.py <工程目录>                    # 探测已有工程
python quench.py new "8位带进位加法器"           # 一句话生成
python quench.py retry --from-file 需求.txt      # 生成 + 多轮自修
python quench.py rounds <工程目录>               # 翻看每一轮的证据
python quench.py bench --suite suites/basic.txt  # 批量跑并出报告
```

---

## API key 怎么处理（关于钱）

**这个仓库里没有任何 key，也永远不会有。**

- 网页里点右下角「⚙ API」自己填，**只存在你自己的浏览器**（localStorage）
- 或写到 `quench/api_key.txt`（已在 `.gitignore` 里）
- 或设环境变量 `DEEPSEEK_API_KEY`

**谁的请求花谁的钱。** 探测已有文件完全免费且离线。

---

## 安全边界（请认真看）

工作台默认只监听 `127.0.0.1`，本机使用**不需要口令**。
一旦对外监听（`--host 0.0.0.0`）会**强制要求访问口令**，并加目录围栏。

**但残余风险是存在的**，实测确认过：

| 手法 | 结果 |
|---|---|
| `$system("...")` 执行系统命令 | ❌ 被 iverilog 禁用 —— 不能直接 RCE |
| `$fopen("/tmp/x","w")` 写文件 | ⚠️ **成功** |
| 死循环仿真 | ✅ 被超时掐断 |

**结论：拿到口令的人可以在服务器上以运行服务的那个用户身份读写文件。**
给一两个人用可以接受；**给一群人用、或者服务器上有敏感数据，不行**。

详见 [部署与安全说明.md](desk-app/部署与安全说明.md)。

---

## 工程配置（`project.json`）

每个被测工程一个 `project.json`，这是唯一约定：

```json
{
  "version": 1,
  "name": "工程名",
  "top": "dut",
  "dut": "dut.v",
  "tb": "tb_dut.v",
  "vcd": "dut.vcd",
  "timeout": 240,
  "distro": "Ubuntu-24.04"
}
```

完整契约（含校验规则和错误码）见 [PROJECT-CONFIG.md](desk-app/PROJECT-CONFIG.md)。

---

## 目录里都有什么

```
desk-app/          工作台（网页 + 本地服务）
  server.py          本地服务：探测、选工程、拖文件、API 转发
  refresh.py         五步探测（真跑 EDA 工具）
  doctor.py          环境自检
  build.py           把界面构建成单文件
  index.html         界面源码
  tools/             开发期用的一次性脚本（普通使用者不用碰）

quench/            生成引擎
  quench.py          命令行入口
  quench_loop.py     多轮自修主循环
  quench_guard.py    护栏：削弱检测、指纹、阴性对照
  quench_classify.py 8 类错误分类
  quench_report.py   质量报告
  suites/            测试用例集
```

---

## 测试

```bash
cd quench  && python test_loop.py && python test_parse.py
cd ../desk-app && python test_step1.py && python test_project_config.py
```

覆盖了护栏、指纹稳定性、存档容错、错误分类（含真实 EDA 工具输出）等。
分类器的测试是**真的去调 iverilog / verilator / yosys 造错误**，
而不是拿手写字符串糊弄 —— 早期版本只测手写样本，
结果真实输出里的 yosys 流程日志把"锁存器"和"位宽"全部误判了。

---

## License

MIT
