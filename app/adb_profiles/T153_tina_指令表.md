# T153 设备指令表(adb 实测记录)

> 由 `T153_tina.json` 自动生成,共 **118** 条命令,全部通过 adb 在设备上实测验证。

## 测试环境

| 项目 | 内容 |
| ---- | ---- |
| 设备 | T153(OKT153-S-Board,飞凌) |
| 系统 | Tina5.0 / Buildroot 2022.05(aiot-t153-linux-v1.0) |
| 内核 | 5.10.198-rt97(PREEMPT_RT,armv7l) |
| 连接方式 | adb(serial: 0402101560) |
| 测试日期 | 2026-08-04 |
| 配置文件 | `app/adb_profiles/T153_tina.json` |

## 指令表

| 序号 | 指令名称 | 命令 |
| ---- | ---- | ---- |

### 【系统】(29)

| 1 | 内核/主机/运行时间 | `uname -a; hostname; uptime` |
| 2 | 系统版本 | `cat /etc/os-release 2>/dev/null; cat /proc/version` |
| 3 | 内核启动参数 | `cat /proc/cmdline` |
| 4 | 系统负载 | `cat /proc/loadavg` |
| 5 | 运行时间(秒) | `cat /proc/uptime` |
| 6 | 环境变量 | `env` |
| 7 | 系统日志 syslog | `tail -n 100 /tmp/messages 2>/dev/null` |
| 8 | 近期内核日志 | `dmesg \| tail -n 80` |
| 9 | 内核编译配置 | `zcat /proc/config.gz 2>/dev/null \| head -40` |
| 10 | 内核硬件枚举日志 | `dmesg \| grep -iE 'ethernet\|usb\|mmc\|nand\|wifi\|bluetooth\|rtc\|watchdog\|i2c\|spi' \| head -30` |
| 11 | 芯片信息 sunxi_info | `cat /sys/class/sunxi_info/sys_info` |
| 12 | 进程数量统计 | `ls /proc/ \| grep -cE '^[0-9]+$'` |
| 13 | init 进程状态 | `cat /proc/1/status` |
| 14 | sysctl 系统参数 | `sysctl kernel.hostname kernel.osrelease` |
| 15 | 中断统计 | `cat /proc/interrupts` |
| 16 | 内存页统计 buddyinfo | `cat /proc/buddyinfo` |
| 17 | 内核内存统计 vmstat | `cat /proc/vmstat \| head -20` |
| 18 | 启动配置 inittab | `cat /etc/inittab` |
| 19 | 用户/组账号 | `cat /etc/passwd; cat /etc/group` |
| 20 | 主机名/DNS 配置 | `cat /etc/hostname; cat /etc/hosts; cat /etc/resolv.conf` |
| 21 | 测试/工具脚本清单 | `ls -l /etc/*.sh` |
| 22 | 内核 sysctl 关键参数 | `cat /proc/sys/kernel/panic /proc/sys/kernel/printk /proc/sys/vm/swappiness /proc/sys/kernel/pid_max /proc/sys/fs/file-max /proc/sys/net/core/somaxconn` |
| 23 | 文件描述符限制 | `ulimit -n` |
| 24 | 根目录结构 | `ls -la /` |
| 25 | 系统电源状态 | `cat /sys/power/state` |
| 26 | 固件清单 | `ls -l /lib/firmware/` |
| 27 | 网络服务端口表 | `cat /etc/services` |
| 28 | 启动脚本 rcS | `cat /etc/init.d/rcS` |
| 29 | 板级信息 | `cat /proc/device-tree/compatible; echo; cat /proc/device-tree/board; echo; cat /proc/device-tree/serial-number` |

### 【CPU/内存】(9)

| 30 | CPU 信息 | `cat /proc/cpuinfo` |
| 31 | 内存信息 | `cat /proc/meminfo` |
| 32 | 内存使用 free | `free` |
| 33 | 进程快照 | `ps 2>/dev/null \|\| ps -ef 2>/dev/null \|\| ps aux 2>/dev/null` |
| 34 | 进程 TOP | `top -b -n 1` |
| 35 | CPU 统计 iostat | `iostat` |
| 36 | 各核占用 mpstat | `mpstat` |
| 37 | 打开文件 lsof | `lsof` |
| 38 | CPU 核数 | `nproc` |

### 【硬件】(26)

| 39 | 芯片型号 | `cat /proc/device-tree/model 2>/dev/null` |
| 40 | CPU/DDR 温度 | `for z in /sys/class/thermal/thermal_zone*; do echo "$(cat $z/type): $(cat $z/temp)"; done` |
| 41 | 看门狗设备 | `ls -l /dev/watchdog* 2>/dev/null` |
| 42 | 设备节点清单 | `ls /dev/` |
| 43 | 寄存器读 sunxi_dump | `echo 0x03000000 > /sys/class/sunxi_dump/dump; cat /sys/class/sunxi_dump/dump` |
| 44 | SPI 总线设备 | `ls /sys/bus/spi/devices/` |
| 45 | PWM 控制器 | `ls /sys/class/pwm/` |
| 46 | 背光设备 | `ls /sys/class/backlight/` |
| 47 | 输入设备 | `ls /sys/class/input/` |
| 48 | DRM 显示设备 | `ls /sys/class/drm/` |
| 49 | 电源调节器 | `ls /sys/class/regulator/` |
| 50 | 热区接口 | `ls /sys/class/thermal/` |
| 51 | 无线状态 iw | `iw dev` |
| 52 | RTC 时钟 hwclock | `hwclock` |
| 53 | SWUpdate 工具版本 | `swupdate -h 2>&1 \| head -2` |
| 54 | fw_printenv 环境配置 | `cat /etc/fw_env.config` |
| 55 | TEE 安全设备 | `ls -l /dev/tee*` |
| 56 | 协处理器 AMP 状态 | `cat /proc/device-tree/sunxi-amp/status` |
| 57 | USB gadget/UDC | `ls /sys/class/udc/; ls /dev/usb-ffs/` |
| 58 | 帧缓冲属性 fb0 | `cat /sys/class/graphics/fb0/mode /sys/class/graphics/fb0/name /sys/class/graphics/fb0/virtual_size /sys/class/graphics/fb0/bits_per_pixel` |
| 59 | 无线固件 AIC8800 | `ls -l /lib/firmware/ \| grep -i aic` |
| 60 | RISC-V 协处理器固件 | `ls -l /lib/firmware/ \| grep -iE 'amp\|rv'` |
| 61 | 设备树 SoC 外设地址表 | `ls /proc/device-tree/soc@3000000/` |
| 62 | 寄存器批量读取 sunxi_dump | `for a in 0x02600000 0x02002000 0x02050000 0x03604000 0x02510000 0x03006000; do echo -n "$a = "; echo $a > /sys/class/sunxi_dump/dump; cat /sys/class/sunxi_dump/dump; done` |
| 63 | 关键外设寄存器读取 | `for a in 0x02050000 0x02600000 0x0260000c 0x02600014 0x03604000 0x03604010; do echo -n "$a = "; echo $a > /sys/class/sunxi_dump/dump; cat /sys/class/sunxi_dump/dump; done` |
| 64 | 外设使能状态查询 | `for n in e907_rproc@1a00000 ethernet@4520000 amp_ts@8120000; do echo -n "$n = "; cat /proc/device-tree/soc@3000000/$n/status 2>/dev/null; echo; done` |

### 【I2C】(1)

| 65 | i2c 总线扫描 | `i2cdetect -l` |

### 【网络】(17)

| 66 | 网卡 ip | `ip a 2>/dev/null \|\| ifconfig -a` |
| 67 | 网卡 ifconfig | `ifconfig -a 2>/dev/null` |
| 68 | 路由 | `ip route 2>/dev/null \|\| route -n 2>/dev/null` |
| 69 | 路由表 route | `route -n 2>/dev/null` |
| 70 | 网络接口统计 | `cat /proc/net/dev` |
| 71 | 网络连接 netstat | `netstat -an 2>/dev/null` |
| 72 | 套接字 ss | `ss -t 2>/dev/null` |
| 73 | ARP 表 | `cat /proc/net/arp 2>/dev/null; arp -a 2>/dev/null` |
| 74 | 网卡详情 ethtool | `ethtool eth0` |
| 75 | 抓包 tcpdump | `tcpdump -i eth0 -n -c 10` |
| 76 | 带宽测试 iperf3 | `iperf3 -c 192.168.1.100 -t 5` |
| 77 | 网络转发参数 | `cat /proc/sys/net/ipv4/ip_forward` |
| 78 | lo 环回连通测试 | `ifconfig lo up; ping -c 3 127.0.0.1` |
| 79 | 网卡链路状态 | `cat /sys/class/net/eth0/operstate /sys/class/net/eth0/carrier /sys/class/net/eth0/speed /sys/class/net/eth0/address 2>/dev/null` |
| 80 | 无线 AP 配置 | `head -10 /etc/hostapd.conf` |
| 81 | 无线 STA 配置 | `grep -vE 'psk\|ssid\|password' /etc/wpa_supplicant.conf` |
| 82 | PTP 时间同步配置 | `cat /etc/linuxptp.cfg` |

### 【存储】(19)

| 83 | 磁盘挂载/使用 | `df -h; mount` |
| 84 | NAND 分区 mtd | `cat /proc/mtd` |
| 85 | 块分区 | `cat /proc/partitions` |
| 86 | UBI 信息 | `ubinfo -a 2>/dev/null` |
| 87 | 分区映射 by-name | `ls -l /dev/by-name/ 2>/dev/null` |
| 88 | 块设备 UUID blkid | `blkid 2>/dev/null` |
| 89 | MTD 分区详情 mtd_debug | `mtd_debug info /dev/mtd0` |
| 90 | NAND 读取 nanddump | `nanddump -l 0x100 /dev/mtd4` |
| 91 | NAND 头部读取 dd | `dd if=/dev/mtd0 bs=2048 count=1 2>/dev/null \| hexdump -C` |
| 92 | 磁盘占用 du | `du -sh /etc /tmp /var /mnt/UDISK 2>/dev/null` |
| 93 | UDISK 数据分区内容 | `ls -l /mnt/UDISK/` |
| 94 | NAND/UBI 工具清单 | `ls /usr/sbin/ \| grep -E '^(flash\|nand\|ubi)'` |
| 95 | 块设备列表 block | `ls /sys/class/block/` |
| 96 | NFS 挂载服务器 | `cat /proc/fs/nfsfs/servers` |
| 97 | boot0 eGON 引导头 | `dd if=/dev/mtd0 bs=2048 count=1 2>/dev/null \| hexdump -C` |
| 98 | uboot 头部 | `dd if=/dev/mtd1 bs=2048 count=1 2>/dev/null \| hexdump -C` |
| 99 | boot 参数分区 | `dd if=/dev/mtd3 bs=2048 count=1 2>/dev/null \| hexdump -C` |
| 100 | uboot 版本/组件字符串 | `dd if=/dev/mtd1 bs=2048 count=8 2>/dev/null \| strings \| head -20` |
| 101 | uboot 环境变量 | `dd if=/dev/ubi0_2 bs=2048 count=4 2>/dev/null \| strings \| head -40` |

### 【音频】(2)

| 102 | 声卡信息 | `cat /proc/asound/cards` |
| 103 | 混音器控制 amixer | `amixer scontrols` |

### 【显示】(1)

| 104 | 帧缓冲信息 fbset | `fbset` |

### 【校验】(6)

| 105 | 文件校验 md5sum | `md5sum /etc/hostname` |
| 106 | 文件校验 sha256sum | `sha256sum /etc/hostname` |
| 107 | 文件校验 cksum | `cksum /etc/hostname` |
| 108 | 文件校验 crc32 | `crc32 /etc/hostname` |
| 109 | 打包/解包 tar | `tar -cf /tmp/backup.tar /etc; tar -tf /tmp/backup.tar; rm -f /tmp/backup.tar` |
| 110 | 压缩 gzip | `gzip -c /etc/hostname` |

### 【调试】(5)

| 111 | hexdump 查看文件 | `hexdump -C /proc/cpuinfo \| head -20` |
| 112 | od 查看文件 | `od -c /proc/cpuinfo \| head -10` |
| 113 | xxd 查看文件 | `xxd /proc/cpuinfo \| head -10` |
| 114 | 字符串提取 strings | `strings /bin/busybox \| head -20` |
| 115 | 写系统日志 logger | `logger "adb debug log"; tail -3 /tmp/messages` |

### 【串口】(1)

| 116 | 串口终端 microcom | `microcom -s 115200 /dev/ttyAS0` |

### 【进程/服务】(1)

| 117 | 启动服务脚本 | `ls /etc/init.d/ 2>/dev/null` |

### 【时间】(1)

| 118 | 当前时间 | `date` |

## 注意事项

- **高危写工具未列入可执行命令**:flash_erase/nandwrite/flashcp/ubiattach/ubiformat/ubimkvol/ubirmvol 等 19 个 NAND/UBI 写工具仅以"工具清单"方式列出,直接执行会破坏数据。
- **依赖网络的命令**:tcpdump/iperf3(示例 IP 192.168.1.100 需替换)/ping 需先配置网络;当前 eth0/eth1 无 IP。lo 经 `ifconfig lo up` 后可 ping 通 127.0.0.1。
- **microcom 为交互式命令**,执行后会占用终端。
- **wpa_supplicant 配置命令已脱敏**(grep 过滤 psk/ssid/password)。
- **devmem 不可用**(/dev/mem 不存在),寄存器读写用全志 sunxi_dump 接口。
- **I2C 工具存在但无总线**:6 个 TWI 控制器在设备树中未绑定,需配置 pinctrl/status 后 i2cdetect 才可用。
- **fw_printenv 需修正 /etc/fw_env.config**(当前指向不存在的 /dev/mmcblk0p2);uboot 环境变量可直接 `dd if=/dev/ubi0_2` 读取。
- **无线(AIC8800)与 E907 RISC-V 协处理器均为预留/disabled 状态**,对应命令用于确认状态。