# 메모리 확장성 검증: NEON Read 1/4/8/16 worker

기존 `memory_bench.c`에 선택형 `--kernel neon` Read 경로를 추가했다.
기본 scalar kernel, 기본 배열 크기(Read 256 MiB, Copy/Triad 각 128 MiB),
기본 ROI 2 pass는 유지한다. `--array-mib`, `--passes 1`,
`--initialize-before-switch`는 명시적으로 선택해야 한다.

NEON Read는 double 원소를 모두 읽어 8개의 독립적인 vector accumulator로
누산한다. 각 worker의 checksum을 전체 원소 수와 비교하므로 sampling을
사용하지 않는다. AArch64 intrinsics와 FADD를 사용하고 fast-math는 적용하지
않는다. 다른 아키텍처에서 NEON 선택, NEON Copy/Triad 선택은 거부한다.

Atomic 초기화 옵션은 모든 배열 원소를 **main thread에서** 초기화한 뒤
worker를 생성하고 affinity를 설정한다. 모든 worker가 barrier에 도착한 뒤
worker 0이 Atomic→O3 전환 marker를 실행한다. 두 번째 barrier로 timing warm-up
시작을 동기화한다.
Timing mode에서 warm-up 1회, barrier, ROI를 수행한다. 따라서 page fault 비용을
ROI 밖으로 옮기는 데 더해 초기화 자체도 Atomic에서 실행한다. 이 경로를
pinned-worker first-touch로 표기하지 않는다. 기본 경로는 기존 worker 초기화를
그대로 사용한다.

## 검증 및 코드 근거

```sh
python3 gem5/tests/zena_cmn/test_memory_bench.py
```

이 테스트는 AArch64 cross compiler와 `qemu-aarch64-static`이 있으면 scalar와
NEON의 1/4/8/16 worker checksum을 검사한다(없으면 SKIP). 실제 gem5 timing
검증은 아래 pilot과 구분한다. 별도 native-host scalar 및 기존 Copy/Triad의
초기화 옵션 on/off 결과도 확인했다.

`build/gem5-zena/calibration-memory-pilot-build/disassembly.txt`의 `neon_read`
루프는 128 B당 `LDR Q` 8개, 독립 `FADD` 8개와 loop 명령 4개다. 루프 안에
stack spill이나 pair load는 없다. Warm-up의 반환값을 버리더라도 volatile
FADD assembly가 있으므로 NEON warm-up은 제거되지 않는다. 따라서 64 MiB 1회 읽기의 loop body는
10,485,760개의 AArch64 명령이다. 이 수치는 전체 guest 명령 수나 gem5 micro-op
수와 동일하지 않다.

## Native 실행 명령

workspace 루트에서 실행한다. 출력 디렉터리는 매번 새로운 경로를 지정한다.

```sh
python3 gem5/scripts/zena_cmn/run_smoke.py \
  --gem5 gem5/build/ARM/gem5.opt \
  --kernel build/gem5-zena/linux/vmlinux \
  --bootloader build/gem5-zena/firmware/zena_css16.elf \
  --initrd build/buildroot/arm64/images/rootfs.cpio \
  --output build/gem5-zena/calibration-memory-neon16-read32-atomic-threads \
  --cpus 16 --workers 16 --channels 16 \
  --profile gem5/configs/zena_cmn/profiles/a720ae_public_candidate.yaml \
  --mode read --passes 1 --kernel-mode neon --array-mib 32 \
  --initialize-before-switch --timeout 1800
```

64 MiB aggregate working set은 해당 16-core profile의 합산 data-cache 용량보다
크지만, 그것만으로 DRAM 대역폭 측정이 입증되지는 않는다. ROI의 16 controller
`dramBytesRead/Written`, cache hit/miss, useful bytes와 ROI 경계를 확인해야 한다.
Native DRAM counter는 burst 서비스 경로에서 burst 크기만큼 증가한다.
Useful bandwidth와 DRAM bandwidth를 별도 보고한다. 아래 4점 worker matrix는 반복 수렴 또는 sustained maximum bandwidth 검증을
대신하지 않는다.

초기 64 MiB pilot은 thread 생성이 timing mode에서 수행된 이전 source snapshot을
사용한다. 해당 source와 hash를 `source-snapshot/`에 보존했다. Thread 생성도
Atomic으로 옮긴 다음 32 MiB pilot을 별도
`calibration-memory-neon16-read32-atomic-threads/`에서 실행했다. 크기와 startup을
함께 바꿨으므로 둘의 시간 차이를 한 변수의 개선 효과로 해석하지 않는다.
32 MiB의 경우 특히 실제 DRAM/useful byte 비율로 residency를 확인해야 한다.

초기 64 MiB pilot은 ROI 중간 처리율에서 총 36–37분이 예상되어, 고정 1,800초
budget을 넘기기 전에 명시적으로 중단했다. `early-stop.json`의 판정은
`EARLY_STOP_BUDGET_PROJECTION`이고 harness 결과는 SIGTERM에 따른 FAIL이다.
최종 partial ROI는 304.31 host초, 3,012,261 명령, DRAM read 18,662,144 B다.
이는 완료된 ROI나 timeout 결과가 아니며 PASS로 사용하지 않는다.

32 MiB의 1/4/8/16 worker 실행은 모두 정상 종료, checksum 및 config/DTB
verification PASS다. 완료 판정은 해당 디렉터리의
`result.json`, guest checksum, `roi_summary.json`으로 확인한다. 실패/시간 초과도
부분 증거를 보존한다. 실행 비용을 pilot에서 확인한 뒤 아래 4점 matrix를 완료했다.


## 완료된 32 MiB 측정

| Worker | ROI | Useful GB/s | DRAM Read GB/s | DRAM Read / useful | Host |
|---:|---:|---:|---:|---:|---:|
| 1 | 8.818791 ms | 3.804879 | 3.815054 | 1.002674 | 996.85 s |
| 4 | 2.771670 ms | 12.106217 | 12.181377 | 1.006208 | 1121.91 s |
| 8 | 1.505961 ms | 22.281084 | 22.356390 | 1.003380 | 1175.84 s |
| 16 | 1.023023 ms | 32.799310 | 32.767404 | 0.999027 | 1459.70 s |

완료된 모든 경우에서 16 controller의 read/write counter 존재를 확인했다. DRAM byte
비율이 약 1이므로 단순 cache-only throughput 실험이 아니다. 다만 이 counter는
OS와 background traffic을 포함하며 주소별 필터가 아니다. 16-worker ROI의
98,112 B DRAM write는 Read kernel의 useful output으로 세지 않는다.

Summary JSON/CSV는 `build/gem5-zena/calibration-memory-scaling-summary.*`에
보존한다. 동일한 guest/gem5/profile hash를 확인하고 marker의 정확한 end tick과
ROI 길이에 일치하는 통계 block만 선택했다. SIGUSR1 중간 dump를 최종 ROI로
오인하지 않는다. Host 시간은 병행 실행 조건이므로 독립 실행 처리율 비교가
아니다. Worker는 CPU 0부터 연속 배치하므로 1/4/8/16 worker는 각각
1/1/2/4 cluster를 사용한다. 이는 해당 topology의 worker scaling이며 코어 수만의
독립 효과로 분리한 실험은 아니다. 1 pass 결과로 수렴이나 sustained maximum
bandwidth를 주장하지 않는다.
Mixed read/write 검증은 native backend 증거와 별도로 유지하며 Linux write
completion calibration은 이 Read 실험 범위에 포함하지 않는다.


Summary를 다시 만들 때는 workspace 루트에서 다음 전용 collector를 실행한다.
초기 artifact collector snapshot도 build 디렉터리에 보존했다.
완료된 run의 config/DTB verification PASS, guest checksum, 입력 SHA256 및 16개
controller counter를 확인하며, 미완료 run을 PASS로 바꾸지 않는다.

```sh
python3 gem5/scripts/zena_cmn/summarize_memory_scaling.py
```

`run_smoke.py`의 정상 종료 판정과 config/DTB verification은 별도 단계다.
후자는 각 run에 대해 아래 명령으로 수행한다.

```sh
python3 gem5/scripts/zena_cmn/verify_config.py <run-directory>/m5out
```
