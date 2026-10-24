# Zena AP Ruby CHI 실행

Apollo workspace의 `apollo-qvp` branch는 root `run_gem5_linux.sh`를 통해
Yocto native build와 배포된 raw arm64 `Image`/initramfs/WIC를 사용한다.
`configs/zena_cmn/run_fs.py --kernel`은 ELF64와 raw Image 모두 받는다.
raw Image는 payload를 보존한 ELF wrapper로 로딩하며 원본 SHA를 manifest에
기록한다. 이 경우 kernel symbol 기반 delay/panic hook은 사용할 수 없다.
Linux cache-ID register는 모든 AP profile에 캐시 geometry를 제공한다.
AP ISA release는 `FEAT_PMULL`을 제외한다. 현재 Advanced SIMD decoder
(`isa/formats/neon64.isa`)는 8-bit polynomial PMULL만 구현하고 64-bit
PMULL/PMULL2를 거절하므로, `ID_AA64ISAR0_EL1.AES=1`로 AES만 광고한다.
AES/SHA1/SHA256/CRC32는 유지하며 Linux GPT 검사는 지원되는 CRC 경로를 쓴다.
SVE PMULL 구현은 별도 ISA이며 NEON의 누락된 64-bit 명령을 대체하지 않는다.
`tests/zena_cmn/directed/ap_features.py`는 실제 ID register와
AES/SHA1/SHA256/CRC32 명령을 짧게 검증한다.
Yocto Cortex-A720 loader가 사용하는 STLUR byte/halfword/word/doubleword는
기존 store-release의 ordering/barrier 경로를 재사용한다. 같은 명령군의
LDAPUR word/doubleword decode 폭도 일치시켰다.
위 회귀를 `--stlur`로 실행하면 폭별 인접 byte 보존, 음수 offset, SP/ZR,
LDAPUR zero extension을 검증한다. 이 검사는 multicore weak-order litmus를
대신하지 않으며 release semantics는 기존 STLR 경로에서 상속한다.
Yocto의 `-mcpu=cortex-a720` userspace에는 IFUNC 선택 밖의 SVE 코드도
존재하므로 AP release는 구현된 SVE/SVE2를 128-bit VL로 활성화한다.
이는 Cortex-A720 전체 ISA 지원 선언이 아니다. EL3 monitor의 CPTR.EZ는
SVE를 허용하고 Linux EL2 초기화가 CPTR.TZ를 해제한 뒤 EL1 CPACR.ZEN을
관리한다. `--sve-el0` 회귀는 이 EL 전환과 실제 EL0 SVE/SVE2 명령을
검사한다. `--o3`를 추가하면 documented profile의 custom FU pool에서
검사하며, predicate/integer SIMD/load-store의 기존 native mapping을 유지한다.
AP 부팅 플랫폼은 `0x30030000`, INTID 290에 기존 `MmioVirtIO`와
`VirtIORng`를 연결하고 `virtio,mmio` DT 노드를 생성한다. Linux의
`CONFIG_HW_RANDOM_VIRTIO` 드라이버가 정상 virtqueue/IRQ 경로로 entropy를
받는다. descriptor/data 접근은 system port의 Ruby functional 경로를
통하므로 캐시/버퍼를 조회하지만 timed DMA traffic은 모델링하지 않는다.
이 장치의 gem5 PRNG 출력은 시뮬레이션 기능 검증용이며 production 보안
난수가 아니다. 동일 simulator seed와 동일 실행 순서에서는 재현 가능한
출력을 만들며 실제 하드웨어 entropy 품질이나 처리량을 나타내지 않는다.

아래 `/srv/oss_contrib` 실행 예와 외부 helper 의존성은 참조 workspace용이며,
Apollo 실행 계약은 상위 workspace `doc/apollo-gem5.md`를 따른다.

처음 실행할 때는 [빠른 실행 가이드](quick_start.md)의 준비 검사·짧은 회귀·결과 판정을 따른다.

이 모델은 현재 프로젝트의 Classic 경로와 독립적인 **보정되지 않은 topology proxy**이다. 실제 Cortex-A720AE/DSU/CMN silicon의 성능 모델로 해석하지 않는다.

기존 `build/ARM`의 `USE_MULTIPLE_PROTOCOLS=y`, 선택된 CHI를 그대로 사용한다. 설계 입력의 single-protocol 요구는 사용자 지시에 따라 필수 조건에서 제외했다. 기존 LPDDR5X timing/geometry와 native memory controller를 재사용한다. DRAM 구현은 변경하지 않는다. CHI에는 private L2의 data residency, cache maintenance 및 uncached DMA 순서 보장을 위한 확장이 포함된다.

```sh
# 저장소 루트 /srv/oss_contrib 기준
# 기존 build/ARM 구성을 유지한 증분 빌드
gem5/scripts/zena_cmn/build.sh
# 기존 커널/firmware/initramfs로 대화형 부팅, UART 기본 포트 3456
gem5/scripts/zena_cmn/run_fs.sh
# 다른 터미널
telnet localhost 3456
```

현재 workspace의 Python PyYAML, AArch64 cross GCC, 기존 부팅 산출물을 사용한다. initramfs 생성은 workspace `scripts/test_zena_css.py`의 CPIO helper를 재사용한다.

기본은 16 CPU / 4 cluster / 16 LPDDR5X 채널 / 2 GiB RAM이다. `--channels 4|8|16`은 총 메모리 용량을 유지한다. `--cpus 4`는 별도의 4 CPU firmware를 함께 지정해야 한다. 8 CPU 설정도 지원하나 8 CPU firmware는 기본 제공하지 않는다. 잘못된 firmware CPU count는 실행 전에 거절한다.

`run_fs.py --interleave-bytes 64|512|1024` 또는 profile의
`memory.interleave_bytes`로 DRAM channel striping을 선택한다. 기본은 64 B이며,
channel selector는 `(PA >> log2(interleave_bytes)) & (channels - 1)`이다.
Home selector `(PA >> 6) & 7`과 cache line 64 B는 그대로 유지한다. 따라서
512 B/1 KiB 구성에서는 한 channel stripe에 여러 Home의 cache line이 포함된다.
RAM 2 GiB, channel당 x16, DRAM timing은 바뀌지 않는다. 이 설정은 단순
address striping 비교이며 Thor의 DMC/PHY 또는 address swizzle 재현이 아니다.
실행의 `resolved-profile.json`과 `manifest.json.address_validation`에 설정을
기록한다. Interleave 변경은 checkpoint 계약 변경이므로 새 snapshot을 사용한다.
Native partition 회귀는 `gem5.opt tests/zena_cmn/test_address_map.py`로,
CPU 없는 backend traffic은 `tests/zena_cmn/directed/lpddr5x_backend_traffic.py
--synthetic --channels 16 --interleave-bytes 512`로 검사한다.

일반 shell 부팅은 atomic_noncaching이다. O3 성능 측정은 게스트의 명시적인 switch/warmup/ROI marker가 필요하다. 자동 검증 실행 예:

```sh
python3 gem5/scripts/zena_cmn/run_smoke.py \
  --gem5 gem5/build/ARM/gem5.opt \
  --kernel build/gem5-zena/linux/vmlinux \
  --bootloader build/gem5-zena/firmware/zena_css16.elf \
  --initrd build/buildroot/arm64/images/rootfs.cpio \
  --output build/gem5-zena/cmn-smoke-new \
  --mode coherence --cpus 16 --workers 16 --iterations 2 --eviction-mib 1
```

`--mode read|copy|stream --passes 2|4|8`은 정해진 aggregate working set을 유지한다. read 256 MiB, copy 128 MiB × 2, STREAM Triad 128 MiB × 3이다. 짧은 실행을 위해 working set을 몰래 축소하지 않는다. 이 테스트는 공식 STREAM 제출 결과가 아니다.

각 실행 디렉터리는 명령/입력 SHA256, `config.json`, DTB, resolved profile, UART, 통계, ROI tick 경계와 결과를 보존한다. `roi_summary.json`은 application bytes와 DRAM burst bytes를 구분하며 없는/모호한 카운터는 null로 표시한다. 추가 종료 dump를 ROI와 합산하지 않는다.

구현 및 검증 범위는 [implementation_report.md](implementation_report.md)에 기록한다. 실패 항목의 후속 결과는 [재검증 보고서](revalidation_report.md)에 기록한다. 원본 설계 입력은 프로젝트의 `doc/gem5/`이다.

짧은 정합성 smoke는 `--eviction-mib 1`을 명시한다. `--eviction-mib 64`의 큰 working set은 별도 장시간 검사이며, 1 MiB 실행으로 대용량 eviction을 검증했다고 주장하지 않는다. 원래 64 MiB 16코어 시도는 20분 host 예산 안에 ROI까지 도달하지 못했다.

실행 기본 LQ는 32이다. 설계 입력의 LQ64는 동일 cache-line 응답 집중으로 native O3 writeback 큐 assertion을 재현하여 채택하지 않았다. 자세한 원인과 재현 증거는 보고서에 있다.

### Diskless Atomic boot checkpoint

`run_fs.py --checkpoint-dir /absolute/new-directory`는 guest의 `m5 checkpoint`
이벤트에서 drain/writeback 후 상태를 저장하고 종료한다. 저장은 NPU가 없는
**diskless Atomic boot, ROI 전환 이전**에만 허용한다. disk 장치, timing boot,
O3 전환 이후 checkpoint는 거부한다. 기존/부분 저장 directory도 덮어쓰지 않는다.
완료 표시는 checkpoint directory의 `apollo-checkpoint.json`이며 simulator
저장이 반환된 뒤 atomic rename으로 게시한다. 실패 시 완료 표시는 남기지 않는다.

`--restore /absolute/checkpoint`는 해당 완료 표시와 `m5.cpt`, profile 및
kernel/firmware/initrd hash와 cmdline 일치를 요구한다. `--readfile /host/script`
내용은 System.readfile로 전달하며 restore마다 바꿀 수 있다. 복원은 Atomic
boot 상태에서 재개하고 기존 guest switch/warmup/ROI marker를 그대로 사용한다.
호스트 launcher는 simulator binary/source 및 추가 입력 compatibility를 검증한다.

`tests/zena_cmn/directed/checkpoint_boot.py --manifest <native-json> --outdir <new-dir>`는
실제 AP/Ruby 객체에서 저장/복원한 레지스터와 RAM, 새 readfile 입력, 복원 후 O3
전환과 ROI 종료를 검사하는 baremetal 회귀다. 이 검사가 Linux initramfs 복원이나
실제 제품 장치 전체의 checkpoint 지원을 입증하지는 않는다. diskless boot의
Ruby cache는 Atomic noncaching 상태이며 timing cache snapshot은 지원 범위 밖이다.
