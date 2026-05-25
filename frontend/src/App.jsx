import React, { useState, useEffect, useRef, useCallback } from 'react'
import { ChevronRight, ChevronDown, File, Folder, FolderOpen, X, Check, Terminal, Search, RefreshCw, Download, Plus, Trash2, Send, Loader2, AlertTriangle, Globe, Code2, Cpu, Paperclip, AlertCircle } from 'lucide-react'
import MonacoEditor from '@monaco-editor/react'
import './App.css'

// ---- WebSocket Hook ----
function useAgent(wsUrl) {
  const ws = useRef(null)
  const [connected, setConnected] = useState(false)
  const handlers = useRef({})

  const connect = useCallback(() => {
    if (ws.current?.readyState === WebSocket.OPEN) return
    const sock = new WebSocket(wsUrl)
    sock.onopen = () => setConnected(true)
    sock.onclose = () => { setConnected(false); setTimeout(connect, 3000) }
    sock.onerror = () => sock.close()
    sock.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        handlers.current[msg.type]?.(msg)
      } catch {}
    }
    ws.current = sock
  }, [wsUrl])

  useEffect(() => { connect(); return () => ws.current?.close() }, [connect])

  const send = useCallback((data) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(data))
  }, [])

  const on = useCallback((type, fn) => { handlers.current[type] = fn }, [])

  return { connected, send, on }
}

// ---- File Tree ----
function getFileIcon(name, isDir, isOpen) {
  if (isDir) return isOpen
    ? <FolderOpen size={14} className="file-icon folder-open" />
    : <Folder size={14} className="file-icon folder" />
  const ext = name.split('.').pop().toLowerCase()
  return <File size={14} className={`file-icon ext-${['js','jsx','ts','tsx','py','json','md','css','html','sh'].includes(ext) ? ext : 'default'}`} />
}

function FileTree({ files, selected, onSelect, onRefresh, onDownload }) {
  const [expanded, setExpanded] = useState(new Set(['.']))
  const tree = buildTree(files)

  function buildTree(flatList) {
    const root = {}
    for (const f of flatList) {
      const parts = f.path.split('/')
      let node = root
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i]
        if (!node[part]) node[part] = { __meta: { ...f, name: part, isDir: i < parts.length - 1 || f.type === 'dir' } }
        node = node[part]
      }
    }
    return root
  }

  function toggle(path) {
    setExpanded(s => { const n = new Set(s); n.has(path) ? n.delete(path) : n.add(path); return n })
  }

  function renderNode(node, path = '', depth = 0) {
    return Object.entries(node)
      .filter(([k]) => k !== '__meta')
      .sort(([, av], [, bv]) => {
        const aDir = av.__meta?.isDir; const bDir = bv.__meta?.isDir
        if (aDir && !bDir) return -1; if (!aDir && bDir) return 1
        return 0
      })
      .map(([key, val]) => {
        const meta = val.__meta || { name: key, isDir: false }
        const fullPath = path ? `${path}/${key}` : key
        const isDir = meta.isDir || Object.keys(val).filter(k => k !== '__meta').length > 0
        const isExp = expanded.has(fullPath)
        const isSelected = selected === fullPath

        return (
          <div key={fullPath}>
            <div
              className={`tree-item ${isSelected ? 'selected' : ''}`}
              style={{ paddingLeft: `${depth * 12 + 8}px` }}
              onClick={() => { isDir ? toggle(fullPath) : onSelect(fullPath) }}
            >
              <span className="tree-icon">
                {isDir ? (isExp ? <ChevronDown size={12} /> : <ChevronRight size={12} />) : null}
              </span>
              {getFileIcon(key, isDir, isExp)}
              <span className="tree-label">{key}</span>
              {!isDir && (
                <span className="tree-item-actions">
                  <button className="icon-btn" title="Download" onClick={(e) => { e.stopPropagation(); onDownload(fullPath) }}><Download size={11} /></button>
                </span>
              )}
            </div>
            {isDir && isExp && renderNode(val, fullPath, depth + 1)}
          </div>
        )
      })
  }

  return (
    <div className="file-tree">
      <div className="panel-header">
        <span>Files</span>
        <div className="panel-header-actions">
          <button className="icon-btn" title="Refresh" onClick={onRefresh}><RefreshCw size={12} /></button>
        </div>
      </div>
      <div className="tree-body">
        {files.length === 0
          ? <div className="empty-state">No files in workspace</div>
          : renderNode(tree)
        }
      </div>
    </div>
  )
}

// ---- Output Panel ----
function OutputPanel({ outputs, onRefresh, onDownload, onDelete }) {
  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes}B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  }

  return (
    <div className="output-panel">
      <div className="panel-header">
        <span>Downloads</span>
        <div className="panel-header-actions">
          <button className="icon-btn" title="Refresh" onClick={onRefresh}><RefreshCw size={12} /></button>
        </div>
      </div>
      <div className="output-list">
        {outputs.length === 0
          ? <div className="empty-outputs">No output files yet</div>
          : outputs.map(f => (
            <div className="output-item" key={f.name}>
              <span className="output-name" title={f.name}>{f.name}</span>
              <span className="output-size">{formatSize(f.size)}</span>
              <div className="output-actions">
                <button className="download-btn" onClick={() => onDownload(f.path)}><Download size={11} /> DL</button>
                <button className="delete-output-btn" title="Delete" onClick={() => onDelete(f.name)}><X size={11} /></button>
              </div>
            </div>
          ))
        }
      </div>
    </div>
  )
}

// ---- Tool Views ----
function DiffView({ diff }) {
  const lines = diff.split('\n')
  return (
    <div className="diff-view">
      {lines.map((line, i) => {
        const cls = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('@@') ? 'hunk' : 'ctx'
        return <div key={i} className={`diff-line ${cls}`}>{line || ' '}</div>
      })}
    </div>
  )
}

function ToolCallBadge({ tool, args }) {
  const icons = {
    web_search: <Globe size={13} />,
    run_command: <Terminal size={13} />,
    apply_diff: <Code2 size={13} />,
    search_files: <Search size={13} />,
    fetch_url: <Globe size={13} />,
    copy_to_output: <Download size={13} />,
  }
  const icon = icons[tool] || <Code2 size={13} />

  let argsDisplay = ''
  if (tool === 'web_search') argsDisplay = `"${args.query}"`
  else if (tool === 'run_command') argsDisplay = `$ ${args.command}`
  else if (tool === 'fetch_url') argsDisplay = args.url
  else if (tool === 'apply_diff') argsDisplay = args.path
  else if (tool === 'copy_to_output') argsDisplay = `${args.path} → output/${args.output_name || args.path?.split('/').pop()}`
  else argsDisplay = JSON.stringify(args, null, 2)

  return (
    <div className="tool-badge">
      <span className="tool-badge-icon">{icon}</span>
      <div className="tool-badge-body">
        <div className="tool-badge-header">
          <span className="tool-name">{tool}</span>
        </div>
        <div className="tool-args">{argsDisplay}</div>
        {tool === 'apply_diff' && args.diff && <DiffView diff={args.diff} />}
      </div>
    </div>
  )
}

function ToolResultView({ tool, result }) {
  if (result.cancelled) return (
    <div className="tool-result neutral"><span className="tool-result-content">cancelled</span></div>
  )

  if (tool === 'web_search') {
    if (result.error) return <div className="tool-result error"><span className="tool-result-content">{result.error}</span></div>
    return (
      <div className="search-results">
        {(result.results || []).map((r, i) => (
          <div key={i} className="search-result">
            {r.title && <div className="search-result-title">{r.title}</div>}
            {r.url && <div className="search-result-url">{r.url}</div>}
            <div className="search-result-snippet">{r.snippet}</div>
          </div>
        ))}
      </div>
    )
  }

  if (tool === 'run_command') {
    const hasErr = result.exit_code !== 0
    return (
      <div>
        {(result.stdout || result.stderr) && (
          <div className={`cmd-output ${hasErr ? 'exit-err' : ''}`}>
            {result.stdout && <span>{result.stdout}</span>}
            {result.stderr && <span style={{color: 'var(--red)'}}>{result.stderr}</span>}
          </div>
        )}
        <span className={`exit-code ${hasErr ? 'err' : 'ok'}`}>exit {result.exit_code ?? '?'}</span>
      </div>
    )
  }

  if (tool === 'fetch_url') {
    if (result.error) return <div className="tool-result error"><span className="tool-result-content">{result.error}</span></div>
    return (
      <div className="tool-result neutral">
        <span className="tool-result-content">{(result.content || '').slice(0, 500)}{(result.content?.length > 500) ? '…' : ''}</span>
      </div>
    )
  }

  if (result.error) return <div className="tool-result error"><span className="tool-result-content">{result.error}</span></div>
  if (result.success) {
    const msg = result.output_path
      ? `✓ Saved to output: ${result.output_path}`
      : result.path ? `✓ ${result.path}` : '✓ Done'
    return <div className="tool-result success"><span className="tool-result-content">{msg}</span></div>
  }

  const preview = JSON.stringify(result).slice(0, 200)
  return <div className="tool-result neutral"><span className="tool-result-content">{preview}</span></div>
}

function ApprovalRequest({ tool, args, onApprove, onReject }) {
  let label = tool
  if (tool === 'run_command') label = `Run: ${args.command}`
  else if (tool === 'write_file') label = `Write: ${args.path}`
  else if (tool === 'apply_diff') label = `Edit: ${args.path}`
  else if (tool === 'delete_file') label = `Delete: ${args.path}`

  return (
    <div className="approval-request">
      <div className="approval-header">
        <AlertCircle size={14} />
        <span>Approval Required</span>
      </div>
      <div className="approval-tool">{label}</div>
      {tool === 'apply_diff' && args.diff
        ? <DiffView diff={args.diff} />
        : <pre className="approval-args">{JSON.stringify(args, null, 2)}</pre>
      }
      <div className="approval-actions">
        <button className="approve-btn" onClick={onApprove}><Check size={13} /> Allow</button>
        <button className="reject-btn" onClick={onReject}><X size={13} /> Reject</button>
      </div>
    </div>
  )
}

function MessageBubble({ msg, onApprove, onReject }) {
  if (msg.type === 'user') return (
    <div className="message user-msg">
      <div className="msg-content">
        {msg.content}
        {msg.attachments?.length > 0 && (
          <div className="msg-attachments">
            {msg.attachments.map((a, i) => (
              <span key={i} className="attachment-chip"><Paperclip size={10} /> {a.name}</span>
            ))}
          </div>
        )}
      </div>
    </div>
  )

  if (msg.type === 'tool_call') return <ToolCallBadge tool={msg.tool} args={msg.args} />
  if (msg.type === 'tool_result') return <ToolResultView tool={msg.tool} result={msg.result} />
  if (msg.type === 'approval') return <ApprovalRequest tool={msg.tool} args={msg.args} onApprove={() => onApprove(msg.callId)} onReject={() => onReject(msg.callId)} />

  if (msg.type === 'text') return (
    <div className="message agent-msg">
      <div className="msg-avatar"><Cpu size={13} /></div>
      <div className="msg-content markdown-text">{msg.content}</div>
    </div>
  )

  if (msg.type === 'error') return (
    <div className="message error-msg">
      <AlertTriangle size={14} />
      <span>{msg.content}</span>
    </div>
  )

  return null
}

// ---- Attach Bar ----
function AttachButton({ onAttach }) {
  const inputRef = useRef(null)

  const handleFiles = (e) => {
    Array.from(e.target.files).forEach(file => {
      const reader = new FileReader()
      reader.onload = (ev) => {
        onAttach({ name: file.name, content: ev.target.result, type: file.type })
      }
      reader.readAsText(file)
    })
    e.target.value = ''
  }

  return (
    <button className="attach-btn" onClick={() => inputRef.current?.click()} title="Attach file">
      <input ref={inputRef} type="file" multiple style={{display:'none'}} onChange={handleFiles} />
      <Paperclip size={14} />
    </button>
  )
}

// ---- Main App ----
export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [files, setFiles] = useState([])
  const [outputs, setOutputs] = useState([])
  const [selectedFile, setSelectedFile] = useState(null)
  const [fileContent, setFileContent] = useState('')
  const [activeTab, setActiveTab] = useState('chat')
  const [attachments, setAttachments] = useState([])

  const bottomRef = useRef(null)
  const inputRef = useRef(null)

  const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`
  const { connected, send, on } = useAgent(wsUrl)

  const addMessage = useCallback((msg) => {
    setMessages(m => [...m, { ...msg, id: Date.now() + Math.random() }])
  }, [])

  const refreshFiles = useCallback(async () => {
    try {
      const r = await fetch('/api/files')
      const data = await r.json()
      if (data.files) setFiles(data.files)
    } catch {}
  }, [])

  const refreshOutputs = useCallback(async () => {
    try {
      const r = await fetch('/api/outputs')
      const data = await r.json()
      if (data.files) setOutputs(data.files)
    } catch {}
  }, [])

  useEffect(() => {
    on('text', (msg) => addMessage({ type: 'text', content: msg.content }))
    on('tool_call', (msg) => addMessage({ type: 'tool_call', tool: msg.tool, args: msg.args }))
    on('tool_result', (msg) => {
      addMessage({ type: 'tool_result', tool: msg.tool, result: msg.result })
      if (msg.tool === 'list_files' && msg.result.files) setFiles(msg.result.files)
      if (msg.tool === 'copy_to_output' && msg.result.success) refreshOutputs()
    })
    on('approval_request', (msg) => {
      const callId = msg.call_id
      addMessage({ type: 'approval', tool: msg.tool, args: msg.args, callId })
    })
    on('done', () => { setLoading(false); refreshFiles(); refreshOutputs() })
    on('error', (msg) => { addMessage({ type: 'error', content: msg.content }); setLoading(false) })
  }, [on, addMessage, refreshFiles, refreshOutputs])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  useEffect(() => {
    if (connected) { refreshFiles(); refreshOutputs() }
  }, [connected, refreshFiles, refreshOutputs])

  const openFile = useCallback(async (path) => {
    setSelectedFile(path)
    setActiveTab('editor')
    try {
      const r = await fetch(`/api/file?path=${encodeURIComponent(path)}`)
      const data = await r.json()
      setFileContent(data.content || '')
    } catch {}
  }, [])

  const downloadFile = useCallback((path) => {
    window.open(`/api/download?path=${encodeURIComponent(path)}`)
  }, [])

  const deleteOutput = useCallback(async (name) => {
    try {
      await fetch(`/api/outputs/${encodeURIComponent(name)}`, { method: 'DELETE' })
      refreshOutputs()
    } catch {}
  }, [refreshOutputs])

  const submit = useCallback(() => {
    const text = input.trim()
    if (!text || loading || !connected) return

    let fileContext = ''
    if (attachments.length > 0) {
      fileContext = attachments.map(a => `=== ${a.name} ===\n${a.content}`).join('\n\n')
    }

    addMessage({ type: 'user', content: text, attachments })
    setInput('')
    setAttachments([])
    setLoading(true)
    send({ type: 'message', content: text, file_context: fileContext })
    inputRef.current?.focus()
  }, [input, loading, connected, send, addMessage, attachments])

  const handleApprove = useCallback((callId) => {
    send({ type: 'approval', approved: true, call_id: callId })
  }, [send])

  const handleReject = useCallback((callId) => {
    send({ type: 'approval', approved: false, call_id: callId })
  }, [send])

  const lang = selectedFile ? selectedFile.split('.').pop() : 'plaintext'
  const monacoLang = { js:'javascript', jsx:'javascript', ts:'typescript', tsx:'typescript', py:'python', md:'markdown', sh:'shell', json:'json', html:'html', css:'css' }[lang] || 'plaintext'

  return (
    <div className="app">
      <div className="titlebar">
        <div className="titlebar-left">
          <Code2 size={16} className="logo-icon" />
          <span className="app-name">codesigner</span>
        </div>
        <div className="titlebar-center">
          {selectedFile && activeTab === 'editor' && (
            <span className="active-file">{selectedFile}</span>
          )}
        </div>
        <div className="titlebar-right">
          <div className={`status-dot ${connected ? 'online' : 'offline'}`} />
          <span className="status-text">{connected ? 'connected' : 'reconnecting...'}</span>
        </div>
      </div>

      <div className="workspace">
        <div className="sidebar">
          <FileTree files={files} selected={selectedFile} onSelect={openFile} onRefresh={refreshFiles} onDownload={downloadFile} />
          <OutputPanel outputs={outputs} onRefresh={refreshOutputs} onDownload={downloadFile} onDelete={deleteOutput} />
        </div>

        <div className="main-area">
          <div className="tabs">
            <button className={`tab ${activeTab === 'chat' ? 'active' : ''}`} onClick={() => setActiveTab('chat')}>
              <Cpu size={13} /> Chat
            </button>
            {selectedFile && (
              <button className={`tab ${activeTab === 'editor' ? 'active' : ''}`} onClick={() => setActiveTab('editor')}>
                <File size={13} /> {selectedFile.split('/').pop()}
                <button className="tab-action" onClick={(e) => { e.stopPropagation(); downloadFile(selectedFile) }} title="Download"><Download size={11} /></button>
              </button>
            )}
          </div>

          {activeTab === 'chat' && (
            <div className="chat-area">
              <div className="messages">
                {messages.length === 0 && (
                  <div className="welcome">
                    <Code2 size={32} className="welcome-icon" />
                    <h2>Codesigner</h2>
                    <p>AI coding assistant. Ask me to write, edit, or run code.</p>
                    <div className="suggestions">
                      {['Create a Python web scraper', 'Set up a Node.js project', 'Write a shell script', 'Search the web for docs'].map(s => (
                        <button key={s} className="suggestion" onClick={() => { setInput(s); inputRef.current?.focus() }}>{s}</button>
                      ))}
                    </div>
                  </div>
                )}
                {messages.map(msg => (
                  <MessageBubble key={msg.id} msg={msg} onApprove={handleApprove} onReject={handleReject} />
                ))}
                {loading && (
                  <div className="message agent-msg loading">
                    <div className="msg-avatar"><Cpu size={13} /></div>
                    <div className="thinking"><Loader2 size={13} className="spin" /><span>thinking...</span></div>
                  </div>
                )}
                <div ref={bottomRef} />
              </div>

              <div className="input-area">
                {attachments.length > 0 && (
                  <div className="attach-bar">
                    {attachments.map((a, i) => (
                      <span key={i} className="attachment-chip">
                        <Paperclip size={10} />
                        <span>{a.name}</span>
                        <button onClick={() => setAttachments(prev => prev.filter((_, idx) => idx !== i))}><X size={10} /></button>
                      </span>
                    ))}
                  </div>
                )}
                <div className="input-row">
                  <AttachButton onAttach={a => setAttachments(prev => [...prev, a])} />
                  <textarea
                    ref={inputRef}
                    className="chat-input"
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }}
                    placeholder="Ask Codesigner... (Enter to send, Shift+Enter for newline)"
                    rows={1}
                    disabled={loading}
                  />
                  <button className={`send-btn ${loading ? 'loading' : ''}`} onClick={submit} disabled={loading || !input.trim()}>
                    {loading ? <Loader2 size={15} className="spin" /> : <Send size={15} />}
                  </button>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'editor' && selectedFile && (
            <div className="editor-area">
              <MonacoEditor
                height="100%"
                language={monacoLang}
                value={fileContent}
                theme="vs-dark"
                onChange={v => setFileContent(v)}
                options={{
                  fontSize: 13,
                  fontFamily: "'SF Mono', 'Fira Code', 'Cascadia Code', Consolas, monospace",
                  fontLigatures: true,
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  wordWrap: 'on',
                  lineNumbers: 'on',
                  renderWhitespace: 'none',
                  smoothScrolling: true,
                  cursorSmoothCaretAnimation: 'on',
                  padding: { top: 16, bottom: 16 }
                }}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
