# SI CL1 standalone simulation

`run.py`는 동일한 Zephyr ELF를 QEMU에서 먼저 검사하고 gem5에서 실행하는
CL1 전용 구성이다. AP, RSE 및 CL0를 생성하지 않는다. 4개의 AArch64 PE,
GICv3 View2 주소, PL011, 100 MHz generic timer를 연결한다.

```sh
# /srv/oss_contrib에서 먼저 QEMU CL1 빌드/검증 완료
python3 gem5/scripts/zena_si_cl1/run.py \
  --outdir build/zena/si-cl1/gem5-minor --cpu minor
```

기본 ELF는 `build/zena/si-cl1/zephyr/bin/zephyr.elf`이다. `--outdir`는 새
디렉터리여야 하며 실행 명령, 바이너리 SHA256, 호스트 시간, return code,
UART 로그(`system.platform.terminal`), `stats.txt`, `config.ini`를 보존한다.
`--cpu atomic`은 기능 디버깅 전용이며 성능 결과로 사용하지 않는다.
`--cpu timing`은 단순 timing 대조, 기본 `minor`는 in-order timing proxy다.
기본은 demand-only cache이며 R82AE의 prefetcher를 보정하지 않았다.
실제 R82AE는 8단계 in-order superscalar이고 Minor는 다른 파이프라인이므로
이 결과를 실물 CPU 속도나 WCET로 해석하지 않는다.

## 주소 및 경로

| 영역 | 주소 / 용량 | 모델 경로 |
|---|---|---|
| CL1 LLRAM | `0x140000000` / 8 MiB | L1I 가능, 데이터 NC 강제, L2 우회 |
| 실험 MM SRAM | `0x40000000` / 8 MiB | 64 KiB 4-way L1 → 512 KiB 8-way shared L2 |
| 실험 NC SRAM | `0x48000000` / 8 MiB | NC request, L2 우회 |
| private ITCM | `0x100000000` / 128 KiB/PE | CPU-side router → private memory |
| private DTCM | `0x100100000` / 128 KiB/PE | CPU-side router → private memory |
| GICD / GICR0 | `0x30200000` / `0x30260000` | GICv3 (4 redistributors) |
| PL011 | `0x2a410000` / SPI39 | UART, EOT 종료 |

TCM 128 KiB, CPU 1 GHz, L1 2-cycle tag/data, L2 8-cycle tag/data, SRAM
20 ns와 TCM 1 ns는 실험 가정이다. MM/LLRAM fabric은 32 B/cycle,
TCM router는 16 B/cycle이다. SRAM backing 32 GB/s와 TCM 16 GB/s는
가정이며 byte당 service period가 정수 1 ps tick으로 양자화된다.
실제 내부 SRAM/TCM의 latency/throughput 보정값이 아니다. TCM은 cache flag만 NC로 바꾸는 것이
아니라 L1 **앞**에서 분기하므로 L1/L2를 통과하지 않는다. 각 PE별 backing
store를 분리하고 시스템의 공유 physical memory map에 등록하지 않는다.
SimpleMemory와 router는 실제 TCM 두 bank의 동시 접근/중재 모델은 아니다.

R82AE TRM 0094/0112에 따라 LLRAM을 L2에 연결하지 않는다. Classic cache의
LLRAM write-through를 구현하지 않았으므로 LLRAM **data**를 NC로 강제한다.
명령은 L1I를 이용하고 L1I miss는 별도 LLRAM PoC fabric으로 간다. 이 정책은 실제
LLRAM data cache 성능을 나타내지 않는다. L1/L2 성능은 MM SRAM에서 측정한다.
MM/NC SRAM은 외부 memory interface 시험용 backing이며 실제 온칩 SRAM
매크로/버스 주파수의 보정값이 아니다.

MM 경로는 `L1 → L2XBar → L2 → MM PoC → MM SRAM`이고, LL/NC/IO 경로는
`L1 → L2XBar → SimpleMemDelay → LL PoC → backing/device`다. 두 PoC의
주소 범위를 분리하여 LLRAM이 L2 cache에 들어가지 않도록 한다.
`SimpleMemDelay`는 cache-clean 사전 `tryTiming`과 snoop 전달을 지원하며,
request/response 각 1 ns와 generic queue를 사용한다. 하드웨어 credit의
유한 용량은 모델링하지 않았다. LL PoC는 여러 L1I miss가 한 upstream
port를 공유하므로 downstream snoop filter를 쓰지 않고 broadcast한다.
상위 L2XBar의 per-L1 snoop filter와 MM PoC의 filter는 유지한다.

## 아키텍처 지원 경계

새 `ArmISA.pmsa64`는 기본 비활성이며 기존 AP의 VMSA 경로를 유지한다.
CL1은 Cortex-R82AE r0p1 MIDR을 노출하되 완전한 해당 CPU 모델은 아니다.
PMSAv8-64 EL1/EL2 indexed MPU 32 regions, MAIR, AP/XN, overlap/unmapped
fault, TCM enable/size, DTCM instruction abort를 지원한다. EL2 reset에서
동일 Zephyr의 EL1 전환과 SMP boot-parameter protocol을 실행한다.

Nested stage 2, VMSA 모드, 직접번호 `PRBARn` register encoding, background
region, AT 변환 명령, secure-state 분리, physical lockstep/ECC/RAS는 구현하지
않았다. VM/DC/VMSA 설정은 조용히 무시하지 않고 실행을 중지한다. 현재는
단일 address/security space이며 GIC single-security-state를 사용한다.
R-profile `CNTP`의 guest IRQ 계약은 29, `CNTV`는 27이다. CNTHPS register의
EL2 접근을 이 opt-in 구성에 한정하여 허용한다. DTCM은 reset에 disabled다.
gem5 sysreg metadata가 프로세스 전역이므로 PMSA/VMSA ArmISA를 한 프로세스에
혼합하면 즉시 오류로 종료한다. AP와 CL1은 별도의 프로세스로 실행해야 한다.

## 보호 기능 probe

```sh
mkdir -p build/zena/si-cl1/gem5-pmsa
aarch64-linux-gnu-gcc -nostdlib -static -ffreestanding -fno-builtin \
  -fno-stack-protector -mgeneral-regs-only -march=armv8-a -O2 \
  -Wl,--build-id=none -Wl,-T,gem5/tests/zena_si_cl1/probe.ld \
  gem5/tests/zena_si_cl1/start.S gem5/tests/zena_si_cl1/pmsa.c \
  -o build/zena/si-cl1/gem5-pmsa/probe.elf
python3 gem5/scripts/zena_si_cl1/run.py \
  --elf build/zena/si-cl1/gem5-pmsa/probe.elf \
  --outdir build/zena/si-cl1/pmsa-atomic --cpu atomic \
  --expect-marker PMSA_TEST_PASS
```

정상 종료만으로 보호 검사를 통과한 것은 아니다. UART의 `PMSA_TEST_PASS`,
각 check의 `PASS` 및 `FAIL` 부재를 함께 확인한다.
Probe는 REGIONS/TIDCP의 EL2 trap 및 ISS, PA40, RO/XN/overlap/unmapped,
TCM enable와 실행 권한, MAX CVAL 및 timer 재설정을 검사한다.
같은 probe를 `--cpu minor`로 실행하여 timing 경로의 보호 동작도 검사한다.
컴파일 시 `-DPMSA_TEST_UNSUPPORTED_STAGE2`를 추가한 별도 ELF는 `HCR.VM`을
설정하므로 `Experimental PMSA does not implement stage 2 or VMSA` 오류와
exit 1이 예상 결과다. `tests/zena_si_cl1/mixed_profile.py --elf <ELF>`와
`--vmsa-first` 변형은 혼합 profile이 초기화 중 exit 1로 거부되는지 검사한다.

## ROI 통계

첫 실행의 guest counter begin/end를 `counter_tick × 10000`으로 변환한
schedule을 동일 설정으로 replay한다. 1 THz simulator tick, 100 MHz counter,
epoch 0인 구성에만 이 변환이 유효하다. schedule은 다음 배열 형식이다.

```json
[{"name":"L1-read","begin_tick":100000000,"end_tick":200000000}]
```

`--roi-schedule schedule.json`은 begin 직전에 통계를 reset하고 end에서
dump한다. 실제 stop tick과 dump 순서는 `run-result.json`의 `roi_events`에
남긴다. guest의 두 번째 실행 counter 경계가 첫 실행과 동일한지도 반드시
검사해야 한다. 파일 끝의 자동 exit dump를 ROI 결과로 선택하면 안 된다.
Counter delta와 cache traffic은 모델 근거이며 host elapsed는 실행비용이다.

근거: root `doc/cortex-r82ae-trm/0004-Features.md`, `0093-TCM-memories.md`,
`0094-L1-memory-system.md`, `0112-L2-memory-system.md`, `0403`–`0405`,
`0413`–`0414`; `doc/arm_zena_css_dev_guide/09-programmers-model-for-zena-css.md`;
QEMU `hw/arm/zena-css-si.c`; Zephyr board `firmware/zena-si/zephyr/`.
