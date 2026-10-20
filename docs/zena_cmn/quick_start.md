# Zena CMN 빠른 실행 가이드

모든 명령의 작업 디렉터리는 `/srv/oss_contrib`이다. 기존 Classic 실행과 독립적인 Ruby CHI 구성이며, 실제 A720AE/DSU/CMN 성능으로 보정된 모델은 아니다.

## 1. 준비 상태 확인

현재 workspace에서 확인한 입력 파일은 다음과 같다.

| 용도 | 경로 |
|---|---|
| 기존 ARM + CHI 실행 파일 | `gem5/build/ARM/gem5.opt` |
| Linux kernel | `build/gem5-zena/linux/vmlinux` |
| 4 / 16 CPU boot firmware | `build/gem5-zena/firmware/zena_css4.elf`, `zena_css16.elf` |
| BusyBox 기본 initramfs | `build/buildroot/arm64/images/rootfs.cpio` |
| 적용 profile | `gem5/configs/zena_cmn/profiles/default.yaml` |

```sh
cd /srv/oss_contrib
command -v python3 aarch64-linux-gnu-gcc dtc
python3 -c 'import yaml; print(yaml.__version__)'
test -x gem5/build/ARM/gem5.opt
for asset in build/gem5-zena/linux/vmlinux \
    build/gem5-zena/firmware/zena_css4.elf \
    build/gem5-zena/firmware/zena_css16.elf \
    build/buildroot/arm64/images/rootfs.cpio; do
    test -r "$asset" || exit 1
done
rg 'USE_MULTIPLE_PROTOCOLS|^PROTOCOL=|^RUBY_PROTOCOL_CHI=' \
    gem5/build/ARM/gem5.build/config
sha256sum gem5/build/ARM/gem5.opt build/gem5-zena/linux/vmlinux \
    build/gem5-zena/firmware/zena_css4.elf \
    build/gem5-zena/firmware/zena_css16.elf \
    build/buildroot/arm64/images/rootfs.cpio
```

기존 빌드는 `USE_MULTIPLE_PROTOCOLS=y`, `PROTOCOL="CHI"`를 사용한다. 누락된 부팅 파일을 임의로 내려받거나 다른 platform 이미지로 대체하지 않는다. Guest test 빌드에는 AArch64 cross GCC와 static libc/pthread가 필요하며, harness가 실제 static 링크를 수행한다. `CC`를 설정했다면 AArch64 compiler인지 확인한다.

소스 변경으로 증분 빌드가 필요할 때만, 실행 중인 검증과 다른 빌드가 끝난 뒤 다음을 사용한다. 기존 build configuration을 재설정하지 않는다.

```sh
JOBS=2 gem5/scripts/zena_cmn/build.sh
```

## 2. 가장 짧은 CPU 설정 회귀 검사

```sh
mkdir -p build/gem5-zena
cmn_run=$(mktemp -d "$PWD/build/gem5-zena/cmn-quick.XXXXXX") || exit 1
gem5/build/ARM/gem5.opt --outdir="$cmn_run/guard" \
    gem5/tests/zena_cmn/directed/cpu_writeback_guard.py
cat "$cmn_run/guard/guard-result.json"
```

PASS는 default LQ32 생성과 LQ64 거절을 뜻한다. 시뮬레이션을 실행하는 테스트는 아니다. 기존 실패에서는 동일 cache line의 read 49개가 한 번에 응답하여 native IEW의 48개 writeback 슬롯을 넘었다. 기본값은 LQ32이며, ROB192/IQ128/SQ64는 유지한다. 버퍼 확대나 protocol 패치로 오류를 숨기지 않는다.

## 3. 4 CPU 부팅·O3 전환·정합성·디스크 회귀

다음 명령은 기존 assertion을 재현했던 것과 같은 `iterations=1`, `eviction-mib=1`, 4 CPU/4채널을 사용한다. Disk 도구가 필요하다. 새 임시 디렉터리에 새 이미지 하나만 생성한다.

```sh
command -v truncate mkfs.ext4
cmn_run=$(mktemp -d "$PWD/build/gem5-zena/cmn-disk.XXXXXX") || exit 1
truncate -s 64M "$cmn_run/disk.ext4" || exit 1
mkfs.ext4 -q "$cmn_run/disk.ext4" || exit 1
python3 gem5/scripts/zena_cmn/run_smoke.py \
    --gem5 gem5/build/ARM/gem5.opt \
    --kernel build/gem5-zena/linux/vmlinux \
    --bootloader build/gem5-zena/firmware/zena_css4.elf \
    --initrd build/buildroot/arm64/images/rootfs.cpio \
    --output "$cmn_run/smoke" \
    --cpus 4 --workers 4 --channels 4 \
    --iterations 1 --eviction-mib 1 \
    --disk-image "$cmn_run/disk.ext4" --timeout 1200
cat "$cmn_run/smoke/result.json"
rg 'CMN_ONLINE|CMN_RESULT|CMN_DISK|CMN_EXIT' \
    "$cmn_run/smoke/guest_console.log"
```

`--output`의 디렉터리는 **존재하면 안 된다**. 위처럼 `mktemp`로 만든 부모 아래의 새 `smoke` 경로를 사용한다. 실패한 디렉터리를 지우고 재사용하지 말고 새 실행 경로를 만든다. Disk는 COW overlay로 연결하므로 base image를 덮어쓰지 않는다.

PASS 판정은 guest 정답 문자열 하나가 아니라 harness의 `result.json`을 기준으로 한다. `CMN_RESULT` PASS 이후에도 종료 assertion이 발생한 전력이 있으므로 `CMN_DISK=PASS`, `CMN_EXIT=0`, 정상 simulator 종료 및 ROI 통계 검사가 모두 필요하다. Host timeout은 실패 원인을 조사할 때 로그와 함께 보존한다. 실제 host 시간은 실행별 결과에 기록되며, 2026-10-14의 동일 입력 4 CPU/1 iteration 회귀는 약 415초였다. Host 부하에 따라 달라지므로 실행 시간 상한을 보장하지 않는다.

## 4. 16 CPU / 4 cluster 검사

```sh
cmn_run=$(mktemp -d "$PWD/build/gem5-zena/cmn-linux16.XXXXXX") || exit 1
python3 gem5/scripts/zena_cmn/run_smoke.py \
    --gem5 gem5/build/ARM/gem5.opt \
    --kernel build/gem5-zena/linux/vmlinux \
    --bootloader build/gem5-zena/firmware/zena_css16.elf \
    --initrd build/buildroot/arm64/images/rootfs.cpio \
    --output "$cmn_run/smoke" \
    --cpus 16 --workers 16 --channels 16 \
    --iterations 2 --eviction-mib 1 --timeout 1200
cat "$cmn_run/smoke/result.json"
```

4 CPU에는 반드시 4 CPU firmware를, 16 CPU에는 16 CPU firmware를 사용한다. `--channels 4|8|16`은 전체 RAM 2 GiB를 유지한다. 1 MiB eviction은 짧은 정합성 검사이며 64 MiB 대용량 eviction이나 DRAM 대역폭 포화 검사를 대신하지 않는다.

## 5. 64 MiB 전체 캐시 라인 검사

위 16 CPU 명령에서 `--eviction-mib 1`을 다음 옵션으로 바꾼다.

```sh
--eviction-mib 64 --prefault-before-switch --timeout 3600
```

새 출력 경로를 사용한다. 이 선택 옵션은 Linux의 페이지 할당·zeroing을
Atomic noncaching 단계에서 먼저 수행한다. O3 전환 후 전체 64 MiB의
1,048,576개 cache line을 초기화하고, ROI에서 변경·읽기·checksum을
검사한다. line당 8 B word 하나를 확인하며 모든 byte를 검사하는 memtest는
아니다. 성능 workload 또는 direct-timing과는 함께 사용할 수 없다.
기본 O3 first-touch 경로를 확인하려면 이 옵션을 생략한다.
두 준비 방식을 동일 조건의 성능 비교로 해석하지 않는다.

주의: 2026-10-14 실행은 약 58분에 전체 line checksum/ROI를 완료했지만,
그 뒤 정상 종료 전에 3600초 제한에 도달했다. 따라서 이 대용량 명령은
**전체 실행 PASS가 확인된 빠른 smoke가 아니다**. 정상 종료까지의 예산은
아직 확정하지 않았으며, 빠른 회귀에는 앞 절의 1 MiB 명령을 사용한다.

`CMN_PROGRESS`로 페이지 준비, warmup, ROI 이후 검증을 구분한다.
[재검증 보고서](revalidation_report.md)에서 실행 결과와 제한을 확인한다.

## 6. 결과 위치와 해석

- `run_manifest.json`: 실제 명령, 입력 SHA256, host timeout.
- `guest_console.log`, `simout`, `simerr`: Linux·simulator 출력과 실패 원인.
- `m5out/config.json`, `system.dts`, `resolved-profile.json`: 생성된 모델·DT·파라미터.
- `m5out/roi-events.json`, `stats.txt`, `roi_summary.json`: 전환/ROI 경계와 측정값. App byte와 DRAM burst byte를 구분한다.
- `result.json`: 전체 실행 판정과 host 시간.

일반 대화형 실행은 `CMN_OUTDIR`를 새 경로로 지정하여 `gem5/scripts/zena_cmn/run_fs.sh`를 사용하고, 다른 터미널에서 `telnet localhost 3456`으로 연결한다. 일반 shell 부팅은 atomic_noncaching이며, O3 실행 증거는 자동 smoke의 guest marker와 host 전환 로그로 확인한다.

현재 native Arm ISA의 CLIDR 노출 때문에 Linux cache sysfs가 `CMN_CACHE=UNAVAILABLE`일 수 있다. DT의 private L2/shared L3와 실제 Ruby 객체 구조가 존재한다는 사실을 sysfs 공유 목록 검증과 혼동하지 않는다. 실제 하드웨어 성능 보정, 대용량 benchmark 수렴 및 미완료 항목은 [구현 보고서](implementation_report.md)를 참조한다.

## 7. 설정·CPU·메모리 진단 회귀

완료된 실행의 설정은 다음 명령으로 재검증한다. 과거 PASS에 hash가 없거나
검증 후 파일이 달라졌다면 새 검증 결과가 필요하다.

```sh
python3 gem5/scripts/zena_cmn/verify_config.py "$cmn_run/smoke/m5out"
```

CPU 의존성·division·L1 load/store12개 case는 다음과 같이 실행한다.
새 출력 경로를 사용하며, 실행 PASS는 하드웨어 timing 일치 판정이 아니다.

```sh
cmn_run=$(mktemp -d "$PWD/build/gem5-zena/cmn-execution.XXXXXX") || exit 1
python3 gem5/scripts/zena_cmn/run_roofline.py \
    --gem5 gem5/build/ARM/gem5.opt \
    --kernel build/gem5-zena/linux/vmlinux \
    --bootloader build/gem5-zena/firmware/zena_css16.elf \
    --initrd build/buildroot/arm64/images/rootfs.cpio \
    --profile gem5/configs/zena_cmn/profiles/a720ae_public_candidate.yaml \
    --execution-checks --timeout 1200 --output "$cmn_run/execution"
python3 gem5/scripts/zena_cmn/analyze_calibration.py "$cmn_run/execution"
python3 gem5/scripts/zena_cmn/verify_config.py "$cmn_run/execution/m5out"
```

각 진단의 짧은 실행 명령과 해석 범위:

- [I-side byte 및 topology 회귀](coherency_regressions.md)
- [Packet/flit 및 L2 trace 분해](fabric_accounting.md)
- [CPU 연산별 qualification](a720ae_execution_limits.md)
- [Native write drain 완료](native_drain_completion.md)
- [Linux Read 반복 수렴·channel phase 수집](memory_convergence.md)

Linux Read 대조는 `run_smoke.py`에 `--cpus 4 --workers 4 --channels 16
--mode read --kernel-mode neon --array-mib 16 --initialize-before-switch`를
적용하고4CPU firmware를 사용한다. 같은 binary/profile에서 `--passes 1|2|4`를
각각 실행한다. Pass2의 주소 배치 대조에만 `--channel-phase-spread`를 추가한다.
이것은4CPU 검증용 실험이며16CPU 최대 대역폭 측정이 아니다.

## 20분 이내 Roofline pilot

별도 `run_roofline.py`가 16 CPU Linux에서 CPU 0 하나로 26개 ROI를 실행한다.
[결과·재현 명령](../../../doc/gem5/roofline_20min_results_ko.md)을 따른다.
첫 campaign은 511.24초에 완료했다. 이 수치는 해당 호스트 실측이며 다른 호스트의
완료 시간 보장은 아니다. DRAM cold는 포화 대역폭 roof가 아니며 현재 SLC/mesh는
Zena 하드웨어와 다른 proxy다. 정상 종료는 benchmark `m5_exit`를 뜻한다.

## 공개 자료 기반 성능 후보 실행

`a720ae_public_candidate.yaml`은 문서 기반 FP/SIMD 실행 특성, 4 MiB SLC,
6×4 mesh, L1D/L2 exclusive 근사와 Hynix LPDDR5X timing을 선택한다.
실물 BOM이나 실측 성능에 보정한 설정은 아니다. 기본 profile은 별도로 유지된다.

```sh
cmn_run=$(mktemp -d "$PWD/build/gem5-zena/cmn-calibration.XXXXXX") || exit 1
python3 gem5/scripts/zena_cmn/run_roofline.py \
    --gem5 gem5/build/ARM/gem5.opt \
    --kernel build/gem5-zena/linux/vmlinux \
    --bootloader build/gem5-zena/firmware/zena_css16.elf \
    --initrd build/buildroot/arm64/images/rootfs.cpio \
    --profile gem5/configs/zena_cmn/profiles/a720ae_public_candidate.yaml \
    --calibration --output "$cmn_run/calibration"
python3 gem5/scripts/zena_cmn/verify_config.py "$cmn_run/calibration/m5out"
python3 gem5/scripts/zena_cmn/analyze_calibration.py "$cmn_run/calibration"
```

위 명령은8개 연산/지연 진단이다. 새 출력 경로에서 `--calibration` 대신
`--memory-patterns` 또는 `--load-streams`를 선택하면 각각 prefetch 패턴과
독립 load stream을 측정한다. `--slc-check`의 checksum PASS와 source 판정은
별개이며, `--slc-check --slc-trace`는 ROI의 주소별 native trace를 남긴다.
보고서의 SLC source 실험은 prefetch-off profile 복사본을 사용했다.

```sh
sed 's/prefetch: stride/prefetch: "off"/' \
    gem5/configs/zena_cmn/profiles/a720ae_public_candidate.yaml \
    > "$cmn_run/public-prefetch-off.yaml"
```

SLC 실행에서는 위 runner의 profile을 이 파일로, mode를
`--slc-check --slc-trace`로, output을 새 `"$cmn_run/slc"`로 바꾼다.
완료 후 주소별 증거를 만들려면 다음 순서로 분석한다.

```sh
python3 gem5/scripts/zena_cmn/verify_config.py "$cmn_run/slc/m5out"
python3 gem5/scripts/zena_cmn/analyze_roofline.py "$cmn_run/slc" --no-plot
python3 gem5/scripts/zena_cmn/analyze_slc_trace.py "$cmn_run/slc"
```

Trace 분석에는 해당 binary의 빌드 출력 `Cache_Transitions.cc`가 필요하다.
큰 trace는 실행 디렉터리에 보존하고, 보고서에는 compact JSON/CSV를 사용한다.

설정별 한계와 검증된 차트는
[하드웨어 성능 보정 보고서](../../../doc/gem5/hardware_performance_calibration_report_ko.md),
전체 DRAM Read의 실행 비용과 byte 판정은
[메모리 pilot](memory_scaling_pilot.md)을 확인한다. Simple 기반9cycle L2 진단을
Garnet 시스템 성능 후보와 같은 설정으로 해석하지 않는다.
