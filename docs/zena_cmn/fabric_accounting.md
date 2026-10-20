# Garnet 전송 계약 및 private L2 trace 검증

2026-10-18, 모델 동작을 바꾸지 않는 진단 단계이다. header 제거, flit 폭 확대,
private bypass, cache timing 변경은 포함하지 않는다.

`python3 scripts/zena_cmn/analyze_fabric.py --config CONFIG --stats STATS
--output RESULT`는 실행 후 생성된 config.json과 stats.txt에서 전송 계약을
추출한다. native simulation 설정을 덮어쓰지 않는다. runtime 계약은 별도
JSON으로 남기므로 기존 manifest/검증 도구의 의미도 바꾸지 않는다.

## 실제 계약

- CHI payload 32 B, generic network header 8 B, Garnet flit 32 B.
- `Network.cc`가 payload와 header를 더하고 `NetworkInterface.cc`가 올림하여
  데이터 packet당 2 flit. 64 B line은 2 packet, 즉 wire 128 B이다.
- `CustomMesh`에서 각 DSU가 연결된 RNF router의 단일 ingress link를 config에서
  찾는다. 현재 2 GHz에서 payload 상한은 클러스터당 32 GB/s, 네 곳 합128 GB/s.
- 이는 SLC/DRAM/remote-cluster에서 돌아오는 데이터가 지나가는 cut의 상한이다.
  자체 DSU L3 hit는 같은 router 내부이므로 이 cut을 통과하지 않는다.
  직렬 hop들의 상한을 합산하면 안 되며 공유 경합은 달성 대역폭을 낮춘다.
- 물리 CHI DAT flit의 metadata 배선이나 CMN 내부 전송을 구현했다는 뜻이 아니다.
  이 상한 하나로 기존 Linux 측정값 약32.8 GB/s를 전부 설명할 수 없다.

## native 실행

저장소 루트(gem5) 기준:

```sh
build/ARM/gem5.opt --outdir=../build/gem5-zena/stage3-fabric-framing \
  tests/zena_cmn/directed/fabric_framing.py
python3 -m unittest discover -s tests/zena_cmn -p test_fabric_analysis.py
```

public candidate 기반, prefetch-off, 단일 outstanding, 64 B read 요청64개,
50 us 생성기 state와1 us idle 후 정상 종료한다. 생성기 state 종료는 workload
완료와 별개이므로 종료 원인과 packet/flit 통계, 생성64개 및 응답 완료 read64개/write0개,
read4096 B를 검사한다. `totalReads`는 native `recvTimingResp()`에서 증가한다. 이 검사는 성능
측정이나 임의 주소의 정확성 검사를 대신하지 않는다.

2026-10-18 결과: simTicks51,000,000, 최종 재실행 native hostSeconds2.47.
DAT512 packets/1024 flits, 다른 세 VN256/0/192 packets 및 같은 flit 수로 PASS.
DAT packet 수512는 여러 cache/controller 전달 단계를 합한 것이며 DRAM 요청64개와
동일한 관측 경계가 아니다. 통계는 injected 기준이므로 미수신 packet이 있더라도
완료 증거로 해석하지 않는다. `fabric-result.json` 및 추가 CLI가 만든
`transport-contract.json`은 `../build/gem5-zena/stage3-fabric-framing/`에 있다.

## 기존 trace 분해

CLI에 `--trace TRACE --line 0x81607fc0 --l1 1 --l2 8
--start-tick 51425830501 --end-tick 51425844000`을 추가한다.
config의 controller version이 같은 코어 L1D/L2인지 확인한다. 선택 구간에서
Begin/Done/각 핵심 event는 정확히1개, 응답은 정확히2개여야 한다. 중첩 요청,
누락 응답, L2 downstream read, 순서 불일치를 거부한다. 동일 주소를 재사용한
긴 trace를 자동으로 짝맞추지 않으며 명시적 구간 선택을 결과에 기록한다.
프로토콜 trace의 address와 controller matching에 한정되므로 NI 내부 큐 지연을
직접 측정했다는 뜻은 아니다. 사용자가 잘못 잘라낸 구간 밖의 활동을 복원하지 않는다.

`slc-linux-trace-v5/m5out` config/stats/trace의 위 구간 결과:

| 단계 | cycles |
|---|---:|
| Seq Begin → L1 SendReadShared | 1.998 |
| L1 send → L2 AllocRequest | 6 |
| L2 allocate → DataArrayRead | 2 |
| L2 data read → SendCompData | 6 |
| L2 send → 첫/마지막 L1 CompData | 8 / 9 |
| Seq Begin → Done | 24.998 |

`existing-trace.json`에 원본 tick과 선택 구간이 있다. 단일 실제 transaction이며
unloaded 평균이 아니다. 시퀀서가 출력한24 cycles는 정수 절삭된 값이다.
호스트 테스트6개는 정상 분해와 중복 Begin, 주소 재사용의 구간 요구, 누락 응답,
L2 miss, 잘못된 flit 비율 및 빈 통계 거부를 포함한다.

## 후속 범위

현재 같은 RNF 내부 전달도 Garnet NI/router를 지난다. global Simple 교체는
mesh 전체 의미를 바꾸므로 private handoff 교정으로 쓰지 않는다. 기존 trace의
controller 경계 이후 NI/credit/queue 세분화가 먼저 필요하다. physical DAT 계약과
bounded local-delivery 설계가 확보되기 전에는 모델 변경을 적용하지 않는다.
