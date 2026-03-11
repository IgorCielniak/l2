import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/mem.sl
import ../stdlib/async.sl

# ── Worker words for scheduler tests ─────────────────────────

word worker_a
    1 puti cr
    yield
    3 puti cr
    yield
    5 puti cr
end

word worker_b
    2 puti cr
    yield
    4 puti cr
    yield
    6 puti cr
end

word worker_single
    42 puti cr
end

word main
    # ── task_new / task_status / task_entry_fn / task_stack_base/size ──
    &worker_single task_new

    dup task_status puti cr           # 0 (TASK_READY)
    dup task_stack_base 0 != puti cr  # 1 (non-null)
    dup task_stack_size puti cr       # 8192
    dup task_entry_fn 0 != puti cr    # 1 (non-null fn ptr)
    dup task_data_sp 0 != puti cr     # 1 (non-null)
    dup task_ret_sp puti cr           # 0 (not yet started)

    task_free

    # ── sched_new / sched_task_count ──
    8 sched_new

    dup sched_task_count puti cr      # 0
    dup sched_tasks_ptr 0 != puti cr  # 1 (non-null)

    # ── sched_spawn ──
    &worker_a sched_spawn
    dup sched_task_count puti cr      # 1
    &worker_b sched_spawn
    dup sched_task_count puti cr      # 2

    # ── sched_run (interleaved output) ──
    sched_run                         # prints: 1 2 3 4 5 6

    # ── post-run: verify we returned cleanly ──
    99 puti cr                        # 99

    sched_free

    # ── single-task scheduler (no yield in worker) ──
    4 sched_new
    &worker_single sched_spawn
    sched_run                         # prints: 42
    sched_free

    # ── three workers to test round-robin with more tasks ──
    8 sched_new
    &worker_a sched_spawn
    &worker_b sched_spawn
    &worker_single sched_spawn
    dup sched_task_count puti cr      # 3
    sched_run                         # worker_a:1, worker_b:2, worker_single:42
                                      # worker_a:3, worker_b:4
                                      # worker_a:5, worker_b:6
    sched_free

    100 puti cr                       # 100 (clean exit)
end
