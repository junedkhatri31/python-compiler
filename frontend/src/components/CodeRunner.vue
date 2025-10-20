<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
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

const wsEndpoint = import.meta.env.VITE_BACKEND_WS_URL ?? 'ws://127.0.0.1:8000/ws/run'

const editorOptions = {
  fontSize: 14,
  minimap: { enabled: false },
  automaticLayout: true,
  wordWrap: 'on',
  scrollBeyondLastLine: false,
}

const runLabel = computed(() => (isRunning.value ? 'Running…' : 'Run Code'))

const teardownSocket = (reason) => {
  if (!socketRef.value) return

  const state = socketRef.value.readyState
  if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) {
    socketRef.value.close(1000, reason)
  }
  socketRef.value = null
}

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
        output.value += payload.stdout.replace(/\r\n/g, '\n')
      }
      if (typeof payload.stderr === 'string' && payload.stderr.length > 0) {
        const prefix = output.value.endsWith('\n') || output.value.length === 0 ? '' : '\n'
        output.value += `${prefix}[stderr] ${payload.stderr.replace(/\r\n/g, '\n')}`
      }
      if (typeof payload.message === 'string') {
        statusMessage.value = payload.message
      }
      if (typeof payload.status === 'string' && payload.status !== 'running' && payload.status !== 'ok') {
        statusMessage.value = `${payload.status.toUpperCase()}: ${statusMessage.value}`
      }
      if (typeof payload.exit_code === 'number') {
        statusMessage.value = `Execution finished with exit code ${payload.exit_code}.`
      }
    } catch {
      output.value += event.data
    }
  }

  socket.onerror = () => {
    statusMessage.value = '⚠️ We hit a network issue talking to the execution backend.'
  }

  socket.onclose = () => {
    if (!output.value) {
      output.value = 'Execution finished.'
    }
    isRunning.value = false
    socketRef.value = null
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
      <button
        type="button"
        class="inline-flex items-center gap-2 rounded-md border border-slate-700 bg-emerald-500/90 px-4 py-2 text-sm font-medium text-slate-950 transition hover:bg-emerald-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300 disabled:cursor-not-allowed disabled:opacity-80 disabled:hover:bg-emerald-500/90"
        :disabled="isRunning"
        @click="runCode"
      >
        <span class="text-lg leading-none">▶</span>
        <span>{{ runLabel }}</span>
      </button>
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
        <div class="flex-1 overflow-y-auto bg-slate-950 px-4 py-3 font-mono text-sm text-slate-200 max-h-[60vh] md:max-h-[75vh]">
          <pre class="whitespace-pre-wrap leading-relaxed">{{ output || '— no output yet —' }}</pre>
        </div>
      </section>
    </main>
  </div>
</template>
