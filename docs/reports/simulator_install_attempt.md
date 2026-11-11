# 仿真器安装尝试（S0-T04）

日期：2026-11-11。结论：**未安装成功，RTL 与 SPICE 未执行。**

## 做了什么

用户已同意（D020、D022）用 Homebrew 安装 `icarus-verilog` 与 `ngspice`，并允许它升级下列已装软件：m4、autoconf、cmake、libomp、brotli、libpng、pkgconf、freetype、xorgproto、libx11、libunistring、readline。

```text
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1 \
  brew install icarus-verilog ngspice
```

## 结果

退出码 1。Homebrew 在开始编译之前检查工具链并拒绝继续：

```text
Error: Your Command Line Tools are too outdated.

Update them from Software Update in System Settings.

If that doesn't show you any updates, run:
  sudo rm -rf /Library/Developer/CommandLineTools
  sudo xcode-select --install

Alternatively, manually download them from:
  https://developer.apple.com/download/all/.
You should download the Command Line Tools for Xcode 26.3.

This is a Tier 3 configuration:
  https://docs.brew.sh/Support-Tiers#tier-3
```

## 环境事实

| 项 | 值 |
|---|---|
| macOS | 15.7.6 |
| 机器与 Homebrew | shell 为 arm64，但 `/usr/local` 下是 **Intel 版** Homebrew 7.0.4（在 Rosetta 下运行），Python 也是 x86_64。Homebrew 对 Intel macOS 已不再提供支持，也不再构建预编译包（Tier 3） |
| Xcode | `xcodebuild -version`：Xcode 26.3，build 17C529 |
| Command Line Tools | `pkgutil --pkg-info=com.apple.pkg.CLTools_Executables`：没有安装记录；`/Library/Developer/CommandLineTools` 目录存在 |
| `icarus-verilog` | 没有适用于本机的预编译包，需从源码编译（还需 bison） |
| `ngspice` | 只有 Sonoma 的预编译包 |

## 核对：没有任何副作用

- 12 个"允许升级"的已装软件，安装前后 `brew list --versions` 逐行相同。
- `bison`、`fftw`、`libngspice`、`icarus-verilog`、`ngspice` 均未被安装。
- `iverilog`、`vvp`、`ngspice` 仍不在 PATH 上。

## 没做的事，以及为什么

- 没有执行 Homebrew 建议的 `sudo` 命令：需要用户的密码，且会删除并重装系统级的 Command Line Tools，超出 D020、D022 的授权。
- 没有改用其他安装渠道：`docs/dev/stages/stage0.md` S0-T04 规定安装失败时报告用户，不改用其他渠道。

## 后续

选项见 `DECISIONS.md` 的 Q012。工具装好后运行 `tools/record_simulators.py`，把"未执行"的记录换成真正的运行结果（记录目录按日期区分，旧的"未执行"记录保留，不覆盖）。
