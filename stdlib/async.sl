# Async — Cooperative coroutine scheduler
#
# Provides lightweight cooperative multitasking built on context switching.
# Each task has its own data stack; the scheduler round-robins between
# ready tasks whenever `yield` is called.
#
# Task layout at address `task`:
#   [task +  0]  status      (qword)  0=ready, 1=running, 2=done
#   [task +  8]  data_sp     (qword)  saved data stack pointer (r12)
#   [task + 16]  ret_sp      (qword)  saved return address for resume
#   [task + 24]  stack_base  (qword)  base of allocated stack buffer
#   [task + 32]  stack_size  (qword)  size of allocated stack buffer
#   [task + 40]  entry_fn    (qword)  pointer to the word to execute
#
# Scheduler layout at address `sched`:
#   [sched +  0]  task_count   (qword)
#   [sched +  8]  current_idx  (qword)
#   [sched + 16]  tasks_ptr    (qword)  pointer to array of task pointers
#   [sched + 24]  main_sp      (qword)  saved main data stack pointer
#   [sched + 32]  main_ret     (qword)  saved main return address
#
# Usage:
#   16 sched_new                     # create scheduler with capacity 16
#   &my_worker1 sched_spawn          # spawn task running my_worker1
#   &my_worker2 sched_spawn          # spawn task running my_worker2
#   sched_run                        # run all tasks to completion
#   sched_free                       # clean up
#
# Inside a task word, call `yield` to yield to the next ready task.

import mem.sl

# ── Constants ─────────────────────────────────────────────────

# Default per-task stack size: 8 KiB
macro ASYNC_STACK_SIZE 0 8192 ;

# Task status values
macro TASK_READY   0 0 ;
macro TASK_RUNNING 0 1 ;
macro TASK_DONE    0 2 ;

# ── Task accessors ────────────────────────────────────────────

#task_status [* | task] -> [* | status]
word task_status @ end

#task_set_status [*, task | status] -> [*]
word task_set_status ! end

#task_data_sp [* | task] -> [* | sp]
word task_data_sp 8 + @ end

#task_set_data_sp [*, task | sp] -> [*]
word task_set_data_sp swap 8 + swap ! end

#task_ret_sp [* | task] -> [* | ret]
word task_ret_sp 16 + @ end

#task_set_ret_sp [*, task | ret] -> [*]
word task_set_ret_sp swap 16 + swap ! end

#task_stack_base [* | task] -> [* | base]
word task_stack_base 24 + @ end

#task_stack_size [* | task] -> [* | size]
word task_stack_size 32 + @ end

#task_entry_fn [* | task] -> [* | fn_ptr]
word task_entry_fn 40 + @ end

# ── Scheduler accessors ──────────────────────────────────────

#sched_task_count [* | sched] -> [* | n]
word sched_task_count @ end

#sched_current_idx [* | sched] -> [* | idx]
word sched_current_idx 8 + @ end

#sched_set_current_idx [*, sched | idx] -> [*]
word sched_set_current_idx swap 8 + swap ! end

#sched_tasks_ptr [* | sched] -> [* | ptr]
word sched_tasks_ptr 16 + @ end

#sched_main_sp [* | sched] -> [* | sp]
word sched_main_sp 24 + @ end

#sched_main_ret [* | sched] -> [* | ret]
word sched_main_ret 32 + @ end

# ── Global scheduler pointer (one active at a time) ──────────

# We store the current scheduler pointer in a global cell
# accessible via `mem`. Offset 0 of persistent buffer = scheduler ptr.

#__async_sched_ptr [*] -> [* | ptr]
# Get the global scheduler pointer
word __async_sched_ptr
    mem @
end

#__async_set_sched_ptr [* | sched] -> [*]
# Set the global scheduler pointer
word __async_set_sched_ptr
    mem swap !
end

# ── Task creation ─────────────────────────────────────────────

#task_new [* | fn_ptr] -> [* | task]
# Create a new task that will execute the given word.
word task_new
    >r  # save fn_ptr; R: [fn_ptr]; stack: [*]

    # Allocate task struct (48 bytes)
    48 alloc  # stack: [* | task]

    # Allocate task stack
    ASYNC_STACK_SIZE alloc >r  # R: [fn_ptr, stk_base]; stack: [* | task]

    # status = READY (0)
    dup 0 !

    # stack_base = stk_base
    r@ over 24 + swap !

    # stack_size = ASYNC_STACK_SIZE
    ASYNC_STACK_SIZE over 32 + swap !

    # data_sp = stk_base + ASYNC_STACK_SIZE - 8 (top of stack, aligned)
    r@ ASYNC_STACK_SIZE + 8 - over 8 + swap !

    # ret_sp = 0 (not yet started)
    dup 16 + 0 !

    # entry_fn = fn_ptr
    rdrop r> over 40 + swap !
end

#task_free [* | task] -> [*]
# Free a task and its stack buffer.
word task_free
    dup task_stack_base over task_stack_size free
    48 free
end

# ── Scheduler creation ───────────────────────────────────────

#sched_new [* | max_tasks] -> [* | sched]
# Create a new scheduler with room for max_tasks.
word sched_new
    # Allocate scheduler struct (40 bytes)
    40 alloc         # stack: [*, max_tasks | sched]

    # task_count = 0
    dup 0 !

    # current_idx = 0
    dup 8 + 0 !

    # Allocate tasks pointer array (max_tasks * 8)
    over 8 * alloc
    over 16 + over ! drop   # sched.tasks_ptr = array

    # main_sp = 0 (set when run starts)
    dup 24 + 0 !

    # main_ret = 0
    dup 32 + 0 !

    nip
end

#sched_free [* | sched] -> [*]
# Free the scheduler and all its tasks.
word sched_free
    # Free each task
    dup sched_task_count
    0
    while 2dup > do
        2 pick sched_tasks_ptr over 8 * + @
        task_free
        1 +
    end
    2drop

    40 free
end

# ── Spawning tasks ────────────────────────────────────────────

#sched_spawn [*, sched | fn_ptr] -> [* | sched]
# Spawn a new task in the scheduler.
word sched_spawn
    task_new >r     # save task; R:[task]; stack: [* | sched]

    # Store task at tasks_ptr[count]
    dup sched_tasks_ptr over @ 8 * +   # [sched, &tasks[count]]
    r@ !                                # tasks[count] = task

    # Increment task_count
    dup @ 1 + over swap !

    rdrop
end

# ── Context switch (the core of async) ───────────────────────

#yield [*] -> [*]
# Yield execution to the next ready task.
# Saves current data stack pointer, restores the next task's.
:asm yield {
    ; Save current r12 (data stack pointer) into current task
    ; Load scheduler pointer from mem (persistent buffer)
    lea rax, [rel persistent]
    mov rax, [rax]            ; sched ptr

    ; Get current_idx
    mov rbx, [rax + 8]        ; current_idx
    mov rcx, [rax + 16]       ; tasks_ptr
    mov rdx, [rcx + rbx*8]    ; current task ptr

    ; Save r12 into task.data_sp
    mov [rdx + 8], r12

    ; Save return address: caller's return is on the x86 stack
    ; We pop it and save it in task.ret_sp
    pop rsi                    ; return address
    mov [rdx + 16], rsi

    ; Mark current task as READY (it was RUNNING)
    mov qword [rdx], 0        ; TASK_READY

    ; Find next ready task (round-robin)
    mov r8, [rax]              ; task_count
    mov r9, rbx                ; start from current_idx
.find_next:
    inc r9
    cmp r9, r8
    jl .no_wrap
    xor r9, r9                 ; wrap to 0
.no_wrap:
    cmp r9, rbx
    je .no_other               ; looped back: only one task

    mov r10, [rcx + r9*8]     ; candidate task
    mov r11, [r10]             ; status
    cmp r11, 0                 ; TASK_READY?
    je .found_task
    jmp .find_next

.no_other:
    ; Only one ready task (self): re-schedule self
    mov r10, rdx
    mov r9, rbx

.found_task:
    ; Update scheduler current_idx
    mov [rax + 8], r9

    ; Mark new task as RUNNING
    mov qword [r10], 1

    ; Check if task has a saved return address (non-zero means resumed)
    mov rsi, [r10 + 16]
    cmp rsi, 0
    je .first_run

    ; Resume: restore data stack and jump to saved return address
    mov r12, [r10 + 8]
    push rsi
    ret

.first_run:
    ; First run: set up data stack and call entry function
    mov r12, [r10 + 8]        ; task's data stack

    ; Save our scheduler info so the task can find it
    ; The task entry function needs no args — it uses the stack.

    ; Get entry function pointer
    mov rdi, [r10 + 40]

    ; When the entry returns, we need to mark it done and yield
    ; Push a return address that handles cleanup
    lea rsi, [rel .task_done]
    push rsi
    jmp rdi                    ; tail-call into task entry

.task_done:
    ; Task finished: mark as DONE
    lea rax, [rel persistent]
    mov rax, [rax]             ; sched ptr
    mov rbx, [rax + 8]        ; current_idx
    mov rcx, [rax + 16]       ; tasks_ptr
    mov rdx, [rcx + rbx*8]    ; current task
    mov qword [rdx], 2        ; TASK_DONE

    ; Find next ready task
    mov r8, [rax]              ; task_count
    mov r9, rbx
.find_next2:
    inc r9
    cmp r9, r8
    jl .no_wrap2
    xor r9, r9
.no_wrap2:
    cmp r9, rbx
    je .all_done               ; no more tasks

    mov r10, [rcx + r9*8]
    mov r11, [r10]
    cmp r11, 0                 ; TASK_READY?
    je .found_task2
    cmp r11, 1                 ; TASK_RUNNING? (shouldn't happen)
    je .found_task2
    jmp .find_next2

.all_done:
    ; All tasks done: restore main context
    mov r12, [rax + 24]        ; main_sp
    mov rsi, [rax + 32]        ; main_ret
    push rsi
    ret

.found_task2:
    mov [rax + 8], r9
    mov qword [r10], 1
    mov rsi, [r10 + 16]
    cmp rsi, 0
    je .first_run2
    mov r12, [r10 + 8]
    push rsi
    ret

.first_run2:
    mov r12, [r10 + 8]
    mov rdi, [r10 + 40]
    lea rsi, [rel .task_done]
    push rsi
    jmp rdi
} ;

# ── Scheduler run ─────────────────────────────────────────────

#sched_run [* | sched] -> [* | sched]
# Run all spawned tasks to completion.
# Saves the main context and starts the first task.
:asm sched_run {
    mov rax, [r12]             ; sched ptr (peek, keep on data stack)

    ; Store as global scheduler
    lea rbx, [rel persistent]
    mov [rbx], rax

    ; Save main data stack pointer (sched still on stack)
    mov [rax + 24], r12

    ; Save main return address (where to come back)
    pop rsi
    mov [rax + 32], rsi

    ; Find first ready task
    mov r8, [rax]              ; task_count
    cmp r8, 0
    je .no_tasks

    mov rcx, [rax + 16]       ; tasks_ptr
    xor r9, r9                 ; idx = 0
.scan:
    cmp r9, r8
    jge .no_tasks
    mov r10, [rcx + r9*8]
    mov r11, [r10]
    cmp r11, 0                 ; TASK_READY?
    je .start
    inc r9
    jmp .scan

.start:
    mov [rax + 8], r9          ; set current_idx
    mov qword [r10], 1         ; TASK_RUNNING
    mov r12, [r10 + 8]         ; task's data stack

    mov rdi, [r10 + 40]        ; entry function
    lea rsi, [rel .task_finished]
    push rsi
    jmp rdi

.task_finished:
    ; Task returned — mark done and find next
    lea rax, [rel persistent]
    mov rax, [rax]
    mov rbx, [rax + 8]
    mov rcx, [rax + 16]
    mov rdx, [rcx + rbx*8]
    mov qword [rdx], 2         ; TASK_DONE

    mov r8, [rax]
    mov r9, rbx
.find_next_run:
    inc r9
    cmp r9, r8
    jl .no_wrap_run
    xor r9, r9
.no_wrap_run:
    cmp r9, rbx
    je .all_done_run

    mov r10, [rcx + r9*8]
    mov r11, [r10]
    cmp r11, 0
    je .found_run
    jmp .find_next_run

.all_done_run:
    ; Restore main context
    mov r12, [rax + 24]
    mov rsi, [rax + 32]
    push rsi
    ret

.found_run:
    mov [rax + 8], r9
    mov qword [r10], 1
    mov rsi, [r10 + 16]
    cmp rsi, 0
    je .first_run_entry
    mov r12, [r10 + 8]
    push rsi
    ret

.first_run_entry:
    mov r12, [r10 + 8]
    mov rdi, [r10 + 40]
    lea rsi, [rel .task_finished]
    push rsi
    jmp rdi

.no_tasks:
    ; Nothing to run — restore and return
    mov r12, [rax + 24]
    mov rsi, [rax + 32]
    push rsi
    ret
} ;
