<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { CodeEditor } from 'monaco-editor-vue3'

const defaultSnippet = `# Welcome to the Python runner
print("Hello from your browser-based editor!")

for i in range(3):
    print(f"Iteration {i + 1}")
`

const code = ref(defaultSnippet)
const output = ref('')
const isRunning = ref(false)
const socketRef = ref(null)
const statusMessage = ref('Click “Run” to execute your code.')
const isAwaitingInput = ref(false)
const currentInput = ref('')
const outputPanelRef = ref(null)
const stdinCaptureRef = ref(null)
const isStopping = ref(false)

const wsEndpoint = import.meta.env.VITE_BACKEND_WS_URL ?? 'ws://127.0.0.1:8000/ws/run'

const editorOptions = {
  fontSize: 14,
  minimap: { enabled: false },
  automaticLayout: true,
  wordWrap: 'on',
  scrollBeyondLastLine: false,
}

const runLabel = computed(() => (isRunning.value ? 'Running…' : 'Run Code'))
const displayedOutput = computed(() => (output.value ? output.value : '— no output yet —'))

const focusStdinCapture = () => {
  nextTick(() => {
    stdinCaptureRef.value?.focus()
  })
}

const scrollOutputToBottom = () => {
  nextTick(() => {
    const node = outputPanelRef.value
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  })
}

const resetInputState = () => {
  isAwaitingInput.value = false
  currentInput.value = ''
}

const teardownSocket = (reason) => {
  resetInputState()
  isStopping.value = false
  if (!socketRef.value) return

  const state = socketRef.value.readyState
  if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
    socketRef.value.close(1000, reason)
  }
  socketRef.value = null
}

const appendOutput = (text) => {
  if (!text) return
  output.value += text
  scrollOutputToBottom()
}

const sendStdinPayload = (value) => {
  const socket = socketRef.value
  if (!socket || socket.readyState !== WebSocket.OPEN || isStopping.value) {
    return false
  }
  socket.send(JSON.stringify({ type: 'stdin', data: value }))
  return true
}

const submitCurrentInput = () => {
  const payload = currentInput.value
  const dataToSend = payload.endsWith('\n') ? payload : `${payload}\n`
  const sent = sendStdinPayload(dataToSend)
  if (sent) {
    appendOutput(payload ? `${payload}\n` : '\n')
    statusMessage.value = 'Input sent. Awaiting program output…'
  }
  resetInputState()
}

const sendEofToContainer = () => {
  const socket = socketRef.value
  if (!socket || socket.readyState !== WebSocket.OPEN || isStopping.value) return
  socket.send(JSON.stringify({ type: 'stdin_close' }))
  appendOutput('^D\n')
  statusMessage.value = 'Sent EOF to stdin.'
  resetInputState()
}

const requestStop = () => {
  const socket = socketRef.value
  if (!socket || socket.readyState !== WebSocket.OPEN || isStopping.value) return

  socket.send(JSON.stringify({ type: 'stop' }))
  isStopping.value = true
  resetInputState()
  statusMessage.value = 'Termination requested… waiting for container to stop.'
}

const handleStdinKeydown = (event) => {
  if (!isAwaitingInput.value) {
    return
  }

  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'd') {
    event.preventDefault()
    sendEofToContainer()
    return
  }

  if (event.key === 'Enter') {
    if (event.shiftKey) {
      event.preventDefault()
      currentInput.value += '\n'
      return
    }
    event.preventDefault()
    submitCurrentInput()
    return
  }

  if (event.key === 'Backspace') {
    event.preventDefault()
    if (currentInput.value.length > 0) {
      currentInput.value = currentInput.value.slice(0, -1)
    }
    return
  }

  if (event.key === 'Tab') {
    event.preventDefault()
    currentInput.value += '\t'
    return
  }

  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
    event.preventDefault()
    currentInput.value += event.key
  }
}

const handleCaptureBlur = () => {
  if (isAwaitingInput.value) {
    focusStdinCapture()
  }
}

const handleOutputClick = () => {
  if (isAwaitingInput.value) {
    focusStdinCapture()
  }
}

watch(output, scrollOutputToBottom)
watch(currentInput, () => {
  if (isAwaitingInput.value) {
    scrollOutputToBottom()
  }
})
watch(isAwaitingInput, (value) => {
  if (value) {
    focusStdinCapture()
  } else if (stdinCaptureRef.value) {
    stdinCaptureRef.value.blur()
  }
})

const runCode = () => {
  if (isRunning.value) return

  const trimmed = code.value.trim()
  if (!trimmed) {
    output.value = '⚠️ Add some Python code before running.'
    return
  }

  isRunning.value = true
  output.value = ''
  statusMessage.value = 'Connecting to execution backend…'
  resetInputState()
  isStopping.value = false

  if (socketRef.value) {
    teardownSocket('switching to a new run')
  }

  const socket = new WebSocket(wsEndpoint)
  socketRef.value = socket

  socket.onopen = () => {
    socket.send(
      JSON.stringify({
        code: trimmed,
      }),
    )
    statusMessage.value = 'Code dispatched. Awaiting execution results…'
  }

  socket.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data)
      if (typeof payload.stdout === 'string' && payload.stdout.length > 0) {
        appendOutput(payload.stdout.replace(/\r\n/g, '\n'))
      }
      if (typeof payload.stderr === 'string' && payload.stderr.length > 0) {
        const prefix = output.value.endsWith('\n') || output.value.length === 0 ? '' : '\n'
        appendOutput(`${prefix}[stderr] ${payload.stderr.replace(/\r\n/g, '\n')}`)
      }
      if (payload.stdin_requested) {
        currentInput.value = ''
        isAwaitingInput.value = true
        statusMessage.value =
          'Program awaiting input… type and press Enter (Shift+Enter for newline, Ctrl+D for EOF).'
        focusStdinCapture()
      }
      if (typeof payload.message === 'string') {
        statusMessage.value = payload.message
      }
      if (typeof payload.status === 'string' && payload.status !== 'running' && payload.status !== 'ok') {
        statusMessage.value = `${payload.status.toUpperCase()}: ${statusMessage.value}`
      }
      if (typeof payload.exit_code === 'number') {
        statusMessage.value = `Execution finished with exit code ${payload.exit_code}.`
        isStopping.value = false
      }
    } catch {
      appendOutput(String(event.data))
    }
  }

  socket.onerror = () => {
    statusMessage.value = '⚠️ We hit a network issue talking to the execution backend.'
  }

  socket.onclose = () => {
    isRunning.value = false
    socketRef.value = null
    resetInputState()
    isStopping.value = false
    if (!output.value) {
      output.value = 'Execution finished.'
    }
    if (!/Execution finished|ERROR|TIMEOUT|⚠️/.test(statusMessage.value)) {
      statusMessage.value = 'Connection to execution backend closed.'
    }
  }
}

onBeforeUnmount(() => {
  teardownSocket('component unmounted')
})
</script>

<template>
  <div class="flex min-h-screen flex-col bg-slate-950 text-slate-100">
    <header class="flex items-center justify-between border-b border-slate-800 px-6 py-4">
      <div>
        <h1 class="text-xl font-semibold">Python Code Runner</h1>
        <p class="text-sm text-slate-400">
          Write Python on the left. Your output will appear on the right as soon as execution lands.
        </p>
      </div>
      <div class="flex items-center gap-3">
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-md border border-slate-700 bg-emerald-500/90 px-4 py-2 text-sm font-medium text-slate-950 transition hover:bg-emerald-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300 disabled:cursor-not-allowed disabled:opacity-70 disabled:hover:bg-emerald-500/90"
          :disabled="isRunning || isStopping"
          @click="runCode"
        >
          <span class="text-lg leading-none">▶</span>
          <span>{{ runLabel }}</span>
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-md border border-slate-700 bg-rose-500/90 px-4 py-2 text-sm font-medium text-slate-50 transition hover:bg-rose-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-300 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="!isRunning || isStopping"
          @click="requestStop"
        >
          <span class="text-lg leading-none">■</span>
          <span>Stop</span>
        </button>
      </div>
    </header>

    <main class="flex flex-1 flex-col gap-6 p-6 md:grid md:grid-cols-2">
      <section class="flex min-h-[360px] flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/60 shadow-lg">
        <div class="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <div>
            <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-300">Editor</h2>
            <p class="text-xs text-slate-500">Monaco-powered, ready for Python syntax highlighting.</p>
          </div>
        </div>
        <div class="relative flex-1">
          <CodeEditor
            v-model:value="code"
            language="python"
            theme="vs-dark"
            :options="editorOptions"
            class="h-full w-full"
          />
        </div>
      </section>

      <section class="flex min-h-[360px] flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/60 shadow-lg">
        <div class="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <div>
            <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-300">Output</h2>
            <p class="text-xs text-slate-500">
              {{ statusMessage }}
            </p>
          </div>
        </div>
        <div
          ref="outputPanelRef"
          class="relative flex-1 overflow-y-auto bg-slate-950 px-4 py-3 font-mono text-sm text-slate-200 max-h-[60vh] md:max-h-[75vh]"
          @click="handleOutputClick"
        >
          <pre class="whitespace-pre-wrap leading-relaxed">{{ displayedOutput }}</pre>
          <div
            v-if="isAwaitingInput"
            class="mt-2 flex items-center gap-2 font-mono text-emerald-400"
          >
            <span>&gt;</span>
            <span class="relative flex-1 whitespace-pre-wrap">
              {{ currentInput || '\u00A0' }}
              <span class="ml-1 inline-block h-4 w-[0.4rem] bg-emerald-400 animate-pulse align-middle"></span>
            </span>
          </div>
          <textarea
            ref="stdinCaptureRef"
            class="absolute bottom-0 left-0 h-[1px] w-[1px] opacity-0"
            tabindex="-1"
            @keydown="handleStdinKeydown"
            @blur="handleCaptureBlur"
          ></textarea>
        </div>
      </section>
    </main>
  </div>
</template>
