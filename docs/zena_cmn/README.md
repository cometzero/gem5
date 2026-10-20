# Zena AP Ruby CHI 실행

Apollo workspace의 `apollo-qvp` branch는 root `run_gem5_linux.sh`를 통해
Yocto native build와 배포된 raw arm64 `Image`/initramfs/WIC를 사용한다.
`configs/zena_cmn/run_fs.py --kernel`은 ELF64와 raw Image 모두 받는다.
raw Image는 payload를 보존한 ELF wrapper로 로딩하며 원본 SHA를 manifest에
기록한다. 이 경우 kernel symbol 기반 delay/panic hook은 사용할 수 없다.
Linux cache-ID register는 모든 AP profile에 캐시 geometry를 제공한다.
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
