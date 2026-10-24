// SPDX-License-Identifier: BSD-3-Clause
// Compile the production Consumer against a deterministic clock/event fixture:
// c++ -std=c++20 -Wall -Wextra -Werror -Wno-unused-parameter -Isrc
//     tests/zena_cmn/consumer_dvfs.cc -o /tmp/consumer-dvfs
// This isolates deadline bookkeeping. Full-system tests cover native clocks.
#include <cassert>
#include <functional>
#include <iostream>
#include <string_view>
#include <vector>

#include "base/types.hh"

// Substitute the event manager/clock and assertion logging boundaries. Keep
// the production types and Consumer implementation, including its queue guard.
#define __SIM_CLOCKED_OBJECT_HH__
#define __SIM_EVENTQ_HH__
#define __BASE_LOGGING_HH__
#define gem5_assert(condition, ...) assert(condition)
namespace gem5
{
Tick now = 0;
Tick
curTick()
{
    return now;
}
struct EventQueue
{
};
EventQueue defaultQueue;
EventQueue *currentQueue = &defaultQueue;

EventQueue *
curEventQueue()
{
    return currentQueue;
}

struct QueueScope
{
    EventQueue *previous;
    explicit QueueScope(EventQueue *queue) : previous(currentQueue)
    {
        currentQueue = queue;
    }
    ~QueueScope() { currentQueue = previous; }
};

struct Event
{
    using Priority = int;
    static constexpr Priority Default_Pri = 0;
};

struct EventFunctionWrapper
{
    std::function<void()> callback;
    Tick deadline = 0;
    bool pending = false;
    EventFunctionWrapper(std::function<void()> f, const char *, bool,
                         Event::Priority)
        : callback(f)
    {}
    bool
    scheduled() const
    {
        return pending;
    }
    Tick
    when() const
    {
        return deadline;
    }
};

struct ClockedObject
{
    Tick period = 100;
    Tick anchor = 0;
    EventFunctionWrapper *event = nullptr;
    EventQueue *queue = &defaultQueue;
    EventQueue *
    eventQueue() const
    {
        return queue;
    }
    Tick
    clockPeriod() const
    {
        return period;
    }
    Cycles
    ticksToCycles(Tick ticks) const
    {
        return Cycles((ticks + period - 1) / period);
    }
    Tick
    clockEdge(Cycles cycles = Cycles(0)) const
    {
        Tick edge = now <= anchor
                        ? anchor
                        : anchor + ticksToCycles(now - anchor) * period;
        return edge + cycles * period;
    }
    void
    changePeriod(Tick ticks)
    {
        anchor = clockEdge();
        period = ticks;
    }
    void
    schedule(EventFunctionWrapper &e, Tick tick)
    {
        assert(!e.pending && tick >= now);
        event = &e;
        e.pending = true;
        e.deadline = tick;
    }
    void
    reschedule(EventFunctionWrapper &e, Tick tick, bool)
    {
        assert(e.pending && tick >= now);
        e.deadline = tick;
    }
    void
    runUntil(Tick tick)
    {
        QueueScope owner(queue);
        while (event && event->pending && event->deadline <= tick) {
            assert(event->deadline >= now);
            now = event->deadline;
            event->pending = false;
            event->callback();
        }
        now = tick;
    }
};
} // namespace gem5

#ifndef CONSUMER_SOURCE
#define CONSUMER_SOURCE "mem/ruby/common/Consumer.cc"
#endif
#include CONSUMER_SOURCE

struct Probe : gem5::ruby::Consumer
{
    gem5::ClockedObject &clock;
    std::vector<gem5::Tick> ticks;
    std::function<void()> onWakeup;
    explicit Probe(gem5::ClockedObject &c) : Consumer(&c), clock(c) {}
    void
    wakeup() override
    {
        assert(gem5::curTick() == clock.clockEdge());
        ticks.push_back(gem5::curTick());
        if (onWakeup) {
            onWakeup();
        }
    }
    void
    print(std::ostream &) const override
    {}
};

int
main(int argc, char **argv)
{
    using namespace gem5;
    if (argc != 1) {
        if (argc != 2) {
            return 2;
        }
        ClockedObject clock;
        EventQueue other_queue;
        clock.queue = &other_queue;
        Probe probe(clock);
        // The production ownership assertion must reject both entry points.
        if (std::string_view(argv[1]) == "--wrong-owner-absolute") {
            probe.scheduleEventAbsolute(100);
        } else if (std::string_view(argv[1]) == "--wrong-owner-relative") {
            probe.scheduleEvent(Cycles(1));
        } else {
            return 2;
        }
        std::cerr << "FAIL: a foreign queue scheduled a Consumer\n";
        return 1;
    }
    {
        now = 0;
        ClockedObject clock;
        Probe probe(clock);
        probe.scheduleEventAbsolute(101);
        probe.scheduleEventAbsolute(199); // Same edge, one wakeup.
        probe.scheduleEvent(Cycles(3));
        clock.runUntil(500);
        assert((probe.ticks == std::vector<Tick>{200, 300}));
    }
    {
        now = 150;
        ClockedObject clock;
        Probe probe(clock);
        clock.changePeriod(150); // Edges: 200, 350, 500, ...
        probe.scheduleEventAbsolute(300);
        probe.scheduleEventAbsolute(501);
        clock.runUntil(800);
        assert((probe.ticks == std::vector<Tick>{350, 650}));
    }
    {
        now = 0;
        ClockedObject clock;
        Probe probe(clock);
        probe.scheduleEventAbsolute(300);
        probe.scheduleEventAbsolute(400);
        probe.scheduleEventAbsolute(900);
        clock.runUntil(150);
        clock.changePeriod(500); // Edges: 200, 700, 1200, ...
        clock.runUntil(310);     // 300 was deferred; it must not disappear.
        probe.scheduleEventAbsolute(350);
        probe.scheduleEventAbsolute(360);
        clock.runUntil(1500);
        assert((probe.ticks == std::vector<Tick>{700, 1200}));
        assert(!probe.alreadyScheduled(300));
        assert(!probe.alreadyScheduled(400));
        assert(!clock.event->pending);
    }
    {
        now = 0;
        ClockedObject clock;
        Probe probe(clock);
        probe.scheduleEventAbsolute(300);
        clock.runUntil(150);
        clock.changePeriod(75); // Edges: 200, 275, 350, ...
        clock.runUntil(500);
        assert((probe.ticks == std::vector<Tick>{350}));
    }
    std::cout << "CONSUMER_DVFS_PASS cases=4\n";
}
