# CHI L2 prefetch pollution 단일 클러스터 검증

`directed/prefetch_pollution.py`의 native CHI 대조 실험에서 stride prefetch가
stream 접근 지연을 줄이는 동시에 기존 hot set의 L2 miss를 증가시키는 것을
확인했다. 이는 **단일 클러스터 정책 검증**이며 16코어 Linux 성능이나 실제
A720AE의 pollution 수치를 나타내지 않는다.

## 구성과 검증 경계

`a720ae_public_candidate.yaml`의 cache/memory 설정을 사용하되 4 endpoint,
1 cluster로 줄이고 endpoint 0의 data sequencer에만 `PyTrafficGen`을 연결한다.
16개 DRAM controller, native CHI factory, `exclusive_data` 정책은 유지한다.
두 실행에서 바뀌는 변수는 L2 prefetch `off`/`stride`뿐이다.

- 초기화: 480 KiB hot set과 84 KiB stream 영역을 64 B write로 초기화한다.
- Warm: 동일한 seeded random 순서의 hot set을 두 번 읽는다.
- Interference: 192개 page에서 각각 연속 7 line을 읽는다. 요청 간격은
  20 ns이고 각 burst 사이에 0.5 us를 추가한다.
- Probe: warm과 동일한 순서로 hot set을 한 번 읽는다.
- 각 phase 끝에는 10 us 여유 시간을 둔다. phase별 응답 수를 정확히 확인하고,
  통계 reset/dump로 구간을 분리한다. 두 실행의 trace SHA256은 모두 동일하다.

`PyTrafficGen → MemCheckerMonitor → Ruby data sequencer` 경로에서 monitor의
`warn_only=false`를 사용한다. native TrafficGen은 requestor-ID를 반복한 byte
pattern을 쓰며, 9,024 write 이후 24,384 read 응답을 검사했다. 이는 알려진
write 이후 read 데이터 일관성 검사이며 atomic ordering이나 주소별로 고유한
pattern을 사용한 alias 검증은 아니다.

## 측정 결과

Warm phase의 모든 측정값은 동일하다. 아래 지연은 CPU load-to-use가 아닌
TrafficGen 요청의 평균 응답 지연이다.

| 구간 / 항목 | Prefetch off | Stride |
|---|---:|---:|
| Interference L2 hit | 0 | 403 |
| Interference L2 miss | 1,344 | 941 |
| Interference prefetch issued / useful | 0 / 0 | 520 / 403 |
| Interference L2 downstream read transaction | 1,344 | 1,442 |
| Interference DRAM read bytes | 0 | 6,272 |
| Interference 평균 read 지연 | 39.682 ns | 37.635 ns |
| Probe L2 hit | 6,840 | 6,785 |
| Probe L2 miss | 840 | 895 |
| Probe L2 downstream read transaction | 840 | 895 |
| Probe prefetch issued | 0 | 0 |
| Probe 평균 read 지연 | 13.379 ns | 13.446 ns |
| Probe DRAM read bytes | 0 | 0 |
| 모든 구간 generator retry / retry ticks | 0 / 0 | 0 / 0 |

이 조건에서 prior stream의 prefetch 활성화로 hot probe miss가 55개 증가했다
(+6.55%). Probe 자체는 양쪽 모두 prefetch를 발행하지 않았으므로 probe의
새로운 prefetch 동작과 분리해서 관찰할 수 있다. 평균 probe 지연 증가는
약 0.50%다. 추가 miss는 하위 cache에서 처리되며 DRAM 추가 읽기는 없다.

Stream에서는 prefetch로 지연이 감소했지만 downstream read가 98개 증가하고
DRAM에서 6,272 B를 더 읽었다. `pfIssued - pfUseful`을 정확한 낭비 byte 수나
미사용 eviction 수로 환산하지 않는다. `pfUnused=0`도 pollution 부재를
뜻하지 않는다. Generator retry가 없었으므로 이 결과로 upstream backpressure를
주장하지 않는다. 내부 queue의 평균/최대 점유는 이 실험에서 측정하지 않았다.
주소 배치와 burst 간격을 고정한 재현 실험으로, 일반 workload의 평균 손실이나
hardware 성능 예측으로 확대해서 해석하지 않는다.

## 재현 및 증거

workspace `/srv/oss_contrib`에서 CHI 정책이 빌드된 기존 바이너리를 사용한다.
새 출력 디렉터리를 각각 지정한다.

```sh
out=$(mktemp -d /tmp/zena-prefetch-pollution.XXXXXX)
gem5/build/ARM/gem5.opt --outdir="$out/off" \
  gem5/tests/zena_cmn/directed/prefetch_pollution.py --prefetch off
gem5/build/ARM/gem5.opt --outdir="$out/stride" \
  gem5/tests/zena_cmn/directed/prefetch_pollution.py --prefetch stride
```

각 `pollution-result.json`의 `status=PASS`, phase별 완료 수, `stats.txt`,
`config.json`, trace hash를 확인한다. Stride 실행은 interference의 실제
`pfIssued > 0`도 assert한다. 필수 generator/cache 통계와 16 controller의
DRAM 통계가 없으면 실패한다. `pfUnused`만 native `nozero` 출력 규칙에 따라
0 생략을 허용한다. Off에서는 prefetcher 통계가 없음을 명시적으로 확인한다.

측정 증거는 다음 경로에 보존했다(workspace 상대 경로).

- `build/gem5-zena/calibration-pollution-final-off/`
- `build/gem5-zena/calibration-pollution-final-stride/`
- `build/gem5-zena/calibration-prefetch-pollution-summary.json`

Summary에는 측정 바이너리/fixture SHA256, phase별 값과 차이, trace 동일성,
관측 한계를 기록했다. 이후 parser를 필수 통계 누락 시 실패하도록 강화하고
기존 8개 phase 통계를 다시 읽어 같은 값을 확인했다. Simulation 재실행 없이
parser만 검증했으며 측정 당시와 검증 당시 fixture hash를 별도로 남겼다.
