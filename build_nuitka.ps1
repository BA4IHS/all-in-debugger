# Nuitka standalone 构建脚本（GUI 无控制台）
# 用法：pwsh build_nuitka.ps1
$ErrorActionPreference = "Stop"

# 打包前清理：旧的构建产物（不涉及源码与用户数据）
Remove-Item -Recurse -Force dist, build, main.build -ErrorAction SilentlyContinue

$lines = @(
    "--standalone",
    "--enable-plugin=pyqt6",
    "--windows-console-mode=disable",
    "--assume-yes-for-downloads",
    "--output-dir=dist",
    "--output-filename=all-in-debugger.exe",
    "--include-package=app",
    # 资源目录：DLL/adb 三件套、图标、HID 模板、ADB 型号档案（保持原相对路径）
    "--include-data-dir=app/libs=app/libs",
    "--include-data-dir=app/assets=app/assets",
    "--include-data-files=app/hid_templates.json=app/hid_templates.json",
    "--include-data-dir=app/adb_profiles=app/adb_profiles",
    # qfluentwidgets 自带的图片/字体等非 py 资源
    "--include-package-data=qfluentwidgets",
    "--product-name=all-in-debugger",
    "--product-version=3.8.0.0",
    "--file-version=3.8.0.0",
    "--file-description=all-in-debugger",
    "main.py"
)

python -m nuitka @lines
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "构建完成: dist/main.dist/all-in-debugger.exe"

# 隐私自检：确认用户私有数据未被打包
$leaked = Get-ChildItem dist/main.dist -Recurse -Include config.json, data.json, *.bin -ErrorAction SilentlyContinue
if ($leaked) {
    Write-Host "[警告] 发现隐私文件被打包:" $leaked.FullName
    exit 1
} else {
    Write-Host "[隐私自检] 通过：未包含 config.json / data.json / 串口日志"
}
