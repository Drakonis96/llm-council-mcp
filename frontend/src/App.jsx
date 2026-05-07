import { startTransition, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'

const defaultConfig = {
  council_models: [],
  chairman_enabled: false,
  chairman_model: null,
  council_timeout_ms: 60000,
  frontend_port: 7842,
  log_level: 'INFO',
}

const defaultApiKeyStatus = {
  configured: false,
  source: 'none',
  preview: null,
}

const openRouterApiKeyPlaceholder = 'sk-or-v1-your-key-here'

function buildUniversalMcpSnippet(command = 'uv', projectRoot = '/absolute/path/to/llm-council-mcp') {
  return JSON.stringify(
    {
      mcpServers: {
        'llm-council': {
          command,
          args: ['--directory', projectRoot, 'run', 'python', 'main.py'],
          env: {
            OPENROUTER_API_KEY: openRouterApiKeyPlaceholder,
          },
        },
      },
    },
    null,
    2,
  )
}

const defaultMcpClient = {
  command: 'uv',
  project_root: '/absolute/path/to/llm-council-mcp',
  snippet: buildUniversalMcpSnippet(),
}

const catalogCacheKey = 'llm-council-openrouter-catalog-v1'

function readCatalogCache() {
  if (typeof window === 'undefined') {
    return { models: [], usage: null }
  }

  try {
    const rawPayload = window.localStorage.getItem(catalogCacheKey)
    if (!rawPayload) {
      return { models: [], usage: null }
    }
    const payload = JSON.parse(rawPayload)
    return {
      models: Array.isArray(payload.models) ? payload.models : [],
      usage: payload.usage ?? null,
    }
  } catch {
    return { models: [], usage: null }
  }
}

function writeCatalogCache(models, usage) {
  if (typeof window === 'undefined') {
    return
  }

  try {
    window.localStorage.setItem(
      catalogCacheKey,
      JSON.stringify({ models, usage }),
    )
  } catch {
    // Ignore cache write failures and keep the live in-memory catalog.
  }
}

function clearCatalogCache() {
  if (typeof window === 'undefined') {
    return
  }

  try {
    window.localStorage.removeItem(catalogCacheKey)
  } catch {
    // Ignore cache removal failures.
  }
}

async function readJson(path, options) {
  const response = await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
    },
    ...options,
  })

  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: 'Request failed.' }))
    throw new Error(payload.detail || 'Request failed.')
  }

  return response.json()
}

function getProviderName(model) {
  const slug = model.canonical_slug || model.id || 'unknown/unknown'
  return slug.split('/')[0] || 'unknown'
}

function formatProviderName(provider) {
  return provider
    .split(/[-_]/g)
    .filter(Boolean)
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(' ')
}

function formatContextLength(value) {
  if (!value) {
    return null
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M context`
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(value % 1_000 === 0 ? 0 : 1)}K context`
  }
  return `${value} context`
}

function formatTimeout(timeoutMs) {
  const seconds = timeoutMs / 1000
  return Number.isInteger(seconds) ? `${seconds}s` : `${seconds.toFixed(1)}s`
}

function formatApiKeySource(source) {
  if (source === 'environment') {
    return 'Provided by environment variables'
  }
  if (source === 'keychain') {
    return 'Stored securely in the system keychain'
  }
  if (source === 'legacy-secrets') {
    return 'Stored in legacy .secrets storage'
  }
  return 'No API key configured'
}

function CodeSnippetCard({ eyebrow, title, description, note, code, onCopy, isCopied }) {
  return (
    <article className="snippet-card">
      <div className="snippet-header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h3>{title}</h3>
        </div>
        <button type="button" className="button secondary snippet-copy-button" onClick={onCopy}>
          {isCopied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <p className="snippet-description">{description}</p>
      {note ? <p className="snippet-note">{note}</p> : null}
      <pre className="snippet-block"><code>{code}</code></pre>
    </article>
  )
}

function ModelPicker({
  eyebrow,
  title,
  description,
  models,
  selectedModels,
  onToggle,
  maxSelection,
  mode = 'multiple',
  emptyMessage,
  dropdownLabel,
  panelClassName = '',
  withPanel = true,
  loading = false,
  onOpen,
}) {
  const [searchValue, setSearchValue] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const triggerRef = useRef(null)
  const pickerRef = useRef(null)
  const searchInputRef = useRef(null)
  const deferredSearch = useDeferredValue(searchValue)
  const modelIndex = useMemo(
    () => new Map(models.map((model) => [model.id, model])),
    [models],
  )
  const providerGroups = useMemo(() => {
    const query = deferredSearch.trim().toLowerCase()
    const groupedModels = new Map()

    models.forEach((model) => {
      const provider = getProviderName(model)
      const haystack = [
        formatProviderName(provider),
        model.name,
        model.id,
        model.description,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()

      if (query && !haystack.includes(query)) {
        return
      }

      if (!groupedModels.has(provider)) {
        groupedModels.set(provider, [])
      }
      groupedModels.get(provider).push(model)
    })

    return Array.from(groupedModels.entries())
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([provider, entries]) => ({
        provider,
        entries: [...entries].sort((left, right) => left.name.localeCompare(right.name)),
      }))
  }, [models, deferredSearch])

  const totalVisibleModels = providerGroups.reduce((count, group) => count + group.entries.length, 0)
  const providerCount = providerGroups.length
  const selectedCountLabel = mode === 'single'
    ? selectedModels.length
      ? 'Pinned'
      : 'Not selected'
    : `${selectedModels.length} / ${maxSelection}`
  const dropdownMeta = loading ? 'Loading catalog…' : `${providerCount} providers · ${totalVisibleModels} models`

  useEffect(() => {
    if (!isOpen) {
      return undefined
    }

    onOpen?.()
    searchInputRef.current?.focus()

    function handlePointerDown(event) {
      if (pickerRef.current && !pickerRef.current.contains(event.target)) {
        setIsOpen(false)
      }
    }

    function handleKeyDown(event) {
      if (event.key === 'Escape') {
        setIsOpen(false)
      }
    }

    window.addEventListener('pointerdown', handlePointerDown)
    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('pointerdown', handlePointerDown)
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [isOpen, onOpen])

  function getPinnedBadge(index) {
    return mode === 'single' ? 'Chairman' : `Model ${String.fromCharCode(65 + index)}`
  }

  function handleToggle(modelId) {
    onToggle(modelId)
    if (mode === 'single') {
      setIsOpen(false)
    }
  }

  const dropdownOffset = `${(triggerRef.current?.offsetHeight ?? 72) + 12}px`

  const content = (
    <>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
        </div>
        <span className="selection-count">{selectedCountLabel}</span>
      </div>
      <p className="panel-copy">{description}</p>
      <div className="picker-body" style={{ '--dropdown-offset': dropdownOffset }}>
        <div className={`picker-dropdown${isOpen ? ' open' : ''}`} ref={pickerRef}>
          <button ref={triggerRef} type="button" className="dropdown-trigger" onClick={() => setIsOpen((current) => !current)}>
            <div>
              <span className="dropdown-label">{dropdownLabel}</span>
              <strong>
                {selectedModels.length
                  ? `${selectedModels.length} pinned`
                  : mode === 'single'
                    ? 'Choose a chairman model'
                    : 'Browse and pin council models'}
              </strong>
            </div>
            <span className="dropdown-meta">{dropdownMeta}</span>
          </button>
          {isOpen ? (
            <div className="dropdown-panel">
              <input
                ref={searchInputRef}
                className="search-input"
                type="search"
                value={searchValue}
                onChange={(event) => setSearchValue(event.target.value)}
                placeholder="Search providers and models"
              />
              <p className="catalog-summary">Live OpenRouter catalog grouped by provider.</p>
              {loading ? (
                <div className="catalog-empty">Loading OpenRouter catalog…</div>
              ) : providerGroups.length === 0 ? (
                <div className="catalog-empty">No models available yet. Use Test connection to load the OpenRouter catalog.</div>
              ) : (
                <div className="model-list">
                  {providerGroups.map((group) => (
                    <section key={group.provider} className="provider-group">
                      <div className="provider-heading">
                        <strong>{formatProviderName(group.provider)}</strong>
                        <span>{group.entries.length} models</span>
                      </div>
                      <div className="provider-list">
                        {group.entries.map((model) => {
                          const isSelected = selectedModels.includes(model.id)
                          const disabled = !isSelected && selectedModels.length >= maxSelection
                          return (
                            <div key={model.id} className={`model-row${isSelected ? ' active' : ''}`}>
                              <div className="model-row-copy">
                                <div className="model-row-heading">
                                  <strong>{model.name}</strong>
                                  <span className="provider-pill">{formatProviderName(group.provider)}</span>
                                </div>
                                <p>{model.id}</p>
                                <div className="model-row-meta">
                                  {formatContextLength(model.context_length) ? <span>{formatContextLength(model.context_length)}</span> : null}
                                </div>
                              </div>
                              <button
                                type="button"
                                className={`pin-button${isSelected ? ' active' : ''}`}
                                onClick={() => handleToggle(model.id)}
                                disabled={disabled}
                              >
                                {isSelected ? 'Unpin' : 'Pin'}
                              </button>
                            </div>
                          )
                        })}
                      </div>
                    </section>
                  ))}
                </div>
              )}
            </div>
          ) : null}
        </div>
        <div className="pinned-models">
          {selectedModels.length === 0 ? <span className="empty-token">{emptyMessage}</span> : null}
          {selectedModels.map((modelId, index) => (
            <div key={modelId} className="pinned-card">
              <div className="pinned-card-copy">
                <span className="pinned-badge">{getPinnedBadge(index)}</span>
                <strong>{modelIndex.get(modelId)?.name || modelId}</strong>
                <p>{modelId}</p>
              </div>
              <button type="button" className="icon-button" onClick={() => handleToggle(modelId)}>
                Remove
              </button>
            </div>
          ))}
        </div>
      </div>
    </>
  )

  if (withPanel) {
    return <section className={`panel panel-soft panel-picker ${panelClassName}`.trim()}>{content}</section>
  }

  return <div className={`model-picker-embedded ${panelClassName}`.trim()}>{content}</div>
}

export default function App() {
  const [config, setConfig] = useState(defaultConfig)
  const [apiKeyInput, setApiKeyInput] = useState('')
  const [apiKeyStatus, setApiKeyStatus] = useState(defaultApiKeyStatus)
  const [mcpHealth, setMcpHealth] = useState({ status: 'unknown' })
  const [availableModels, setAvailableModels] = useState(() => readCatalogCache().models)
  const [usage, setUsage] = useState(() => readCatalogCache().usage)
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [mcpClient, setMcpClient] = useState(defaultMcpClient)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [isConfigDirty, setIsConfigDirty] = useState(false)
  const [copiedSnippetId, setCopiedSnippetId] = useState(null)
  const dirtyRef = useRef(false)
  const catalogRequestRef = useRef(null)
  const snippetResetRef = useRef(null)

  useEffect(() => () => {
    if (snippetResetRef.current) {
      window.clearTimeout(snippetResetRef.current)
    }
  }, [])

  function markConfigDirty(nextDirty) {
    dirtyRef.current = nextDirty
    setIsConfigDirty(nextDirty)
  }

  function updateConfig(updater) {
    markConfigDirty(true)
    setConfig((current) => updater(current))
  }

  async function loadCatalog(submittedKey = null) {
    if (catalogRequestRef.current) {
      return catalogRequestRef.current
    }

    const request = (async () => {
      setCatalogLoading(true)
      try {
        const payload = await readJson('/api/test-connection', {
          method: 'POST',
          body: JSON.stringify({ api_key: submittedKey }),
        })

        writeCatalogCache(payload.models, payload.usage)
        startTransition(() => {
          setAvailableModels(payload.models)
          setUsage(payload.usage)
        })

        return payload
      } finally {
        setCatalogLoading(false)
        catalogRequestRef.current = null
      }
    })()

    catalogRequestRef.current = request
    return request
  }

  async function ensureCatalogLoaded() {
    if (!apiKeyStatus.configured || availableModels.length > 0 || catalogLoading) {
      return
    }

    try {
      await loadCatalog(null)
    } catch (loadError) {
      setError(loadError.message)
    }
  }

  async function refreshDashboard(options = {}) {
    const { preserveDirty = true } = options
    const configPayload = await readJson('/api/config')

    startTransition(() => {
      if (!preserveDirty || !dirtyRef.current) {
        setConfig(configPayload.config)
      }
      setApiKeyStatus(configPayload.api_key || defaultApiKeyStatus)
      setMcpClient(configPayload.mcp_client || defaultMcpClient)
      setMcpHealth(configPayload.mcp_health)
    })

    return configPayload
  }

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const configPayload = await refreshDashboard({ preserveDirty: false })
        if (!cancelled && configPayload.api_key_configured && availableModels.length === 0) {
          const modelPayload = await loadCatalog(null).catch(() => null)
          if (modelPayload && !cancelled) {
            return
          }
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError.message)
        }
      }
    }

    load()
    const intervalId = window.setInterval(() => {
      refreshDashboard().catch(() => {})
    }, 5000)

    return () => {
      cancelled = true
      window.clearInterval(intervalId)
    }
  }, [])

  const canSave =
    config.council_models.length >= 2 &&
    config.council_models.length <= 6 &&
    (!config.chairman_enabled || Boolean(config.chairman_model))

  function toggleCouncilModel(modelId) {
    updateConfig((current) => {
      const isSelected = current.council_models.includes(modelId)
      if (isSelected) {
        const nextModels = current.council_models.filter((candidate) => candidate !== modelId)
        const chairmanModel = current.chairman_model === modelId ? null : current.chairman_model
        return { ...current, council_models: nextModels, chairman_model: chairmanModel }
      }
      if (current.council_models.length >= 6) {
        return current
      }
      return { ...current, council_models: [...current.council_models, modelId] }
    })
  }

  function toggleChairmanModel(modelId) {
    updateConfig((current) => ({
      ...current,
      chairman_model: current.chairman_model === modelId ? null : modelId,
    }))
  }

  async function handleTestConnection() {
    setBusy(true)
    setError('')
    setMessage('')

    try {
      const payload = await loadCatalog(apiKeyInput.trim() || null)
      setMessage(`Connection verified. ${payload.model_count} models available.`)
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleSave() {
    setBusy(true)
    setError('')
    setMessage('')

    try {
      const payload = await readJson('/api/config', {
        method: 'POST',
        body: JSON.stringify({
          api_key: apiKeyInput.trim() || null,
          council_models: config.council_models,
          chairman_enabled: config.chairman_enabled,
          chairman_model: config.chairman_model,
          council_timeout_ms: config.council_timeout_ms,
        }),
      })
      setApiKeyInput('')
      markConfigDirty(false)
      startTransition(() => {
        setConfig(payload.config)
        setApiKeyStatus(payload.api_key || defaultApiKeyStatus)
      })
      await refreshDashboard({ preserveDirty: false })
      setMessage(
        payload.restart.succeeded
          ? `Saved ${payload.config.council_models.length} council models and restarted the MCP server.`
          : `Saved ${payload.config.council_models.length} council models. Restart the MCP process if needed.`,
      )
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleStopServer() {
    setBusy(true)
    setError('')
    setMessage('')

    try {
      const payload = await readJson('/api/server/stop', {
        method: 'POST',
      })
      await refreshDashboard({ preserveDirty: true })
      setMessage(payload.stop.succeeded ? 'MCP server stopped.' : payload.stop.message)
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleDeleteApiKey() {
    setBusy(true)
    setError('')
    setMessage('')

    try {
      const payload = await readJson('/api/config/api-key', {
        method: 'DELETE',
      })
      setApiKeyInput('')
      startTransition(() => {
        setApiKeyStatus(payload.api_key || defaultApiKeyStatus)
      })
      if (!(payload.api_key || defaultApiKeyStatus).configured) {
        clearCatalogCache()
        startTransition(() => {
          setAvailableModels([])
          setUsage(null)
        })
      }
      setMessage(payload.message)
    } catch (requestError) {
      setError(requestError.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleCopySnippet(snippetId, snippetText) {
    try {
      await navigator.clipboard.writeText(snippetText)
      setCopiedSnippetId(snippetId)
      if (snippetResetRef.current) {
        window.clearTimeout(snippetResetRef.current)
      }
      snippetResetRef.current = window.setTimeout(() => {
        setCopiedSnippetId(null)
      }, 1800)
    } catch {
      setError('Unable to copy the MCP snippet. Copy it manually from the code block.')
    }
  }

  const apiKeyConfigured = apiKeyStatus.configured
  const apiKeySummaryLabel = apiKeyConfigured ? 'Key fingerprint' : 'Current key'
  const canDeleteApiKey = apiKeyStatus.source === 'keychain' || apiKeyStatus.source === 'legacy-secrets'
  const apiKeyPlaceholder = apiKeyConfigured
    ? 'Enter a new key to replace the current one'
    : 'sk-or-v1-...'
  const mcpSnippetNote = mcpClient.project_root
    ? `Auto-detected project path for this machine: ${mcpClient.project_root}`
    : 'Replace the placeholder path with your local checkout before saving.'

  return (
    <main className="app-shell">
      <div className="orb orb-left" />
      <div className="orb orb-right" />
      <header className="hero">
        <div>
          <p className="eyebrow">LLM Council Control Room</p>
          <h1>Compose the council.</h1>
          <p className="hero-copy">
            Model identities are anonymized to prevent bias. The council always returns Model A, Model B, etc.
          </p>
        </div>
        <div className={`status-badge ${mcpHealth.status}`}>
          <span className="status-dot" />
          <strong>MCP server {mcpHealth.status}</strong>
        </div>
      </header>

      <section className="grid-layout">
        <section className="panel panel-stack credentials-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Credentials</p>
              <h2>OpenRouter access</h2>
            </div>
            <span className={`chip ${apiKeyConfigured ? 'ok' : 'idle'}`}>
              {apiKeyConfigured ? 'API key configured ✓' : 'No API key configured'}
            </span>
          </div>
          <label className="field-label" htmlFor="api-key">OpenRouter API key</label>
          <input
            id="api-key"
            className="text-input"
            type="password"
            value={apiKeyInput}
            onChange={(event) => setApiKeyInput(event.target.value)}
            placeholder={apiKeyPlaceholder}
            autoComplete="off"
          />
          <div className="key-summary-card">
            <div className="key-summary-copy">
              <span className="key-summary-label">{apiKeySummaryLabel}</span>
              <strong>{apiKeyConfigured ? apiKeyStatus.preview : 'Not configured'}</strong>
              <p>{formatApiKeySource(apiKeyStatus.source)}</p>
            </div>
            <button
              type="button"
              className="button danger subtle"
              onClick={handleDeleteApiKey}
              disabled={busy || !canDeleteApiKey}
            >
              {canDeleteApiKey ? 'Delete stored API key' : 'Managed by env'}
            </button>
          </div>
          <div className="action-row">
            <button type="button" className="button secondary" onClick={handleTestConnection} disabled={busy}>
              Test connection
            </button>
            {usage ? (
              <div className="usage-card">
                <span>Remaining credits</span>
                <strong>{usage.limit_remaining ?? 'unlimited'}</strong>
              </div>
            ) : null}
          </div>
        </section>

        <ModelPicker
          eyebrow="Council roster"
          title="Council members"
          description="Pin between two and six models. The pinned order sets the stable anonymized labels for the current session."
          models={availableModels}
          selectedModels={config.council_models}
          onToggle={toggleCouncilModel}
          maxSelection={6}
          emptyMessage="No council models pinned yet."
          dropdownLabel="OpenRouter model catalog"
          panelClassName="council-panel"
          loading={catalogLoading}
          onOpen={ensureCatalogLoaded}
        />

        <section className="panel panel-stack chairman-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Chairman</p>
              <h2>Synthesis mode</h2>
            </div>
            <label className="switch">
              <input
                type="checkbox"
                checked={config.chairman_enabled}
                onChange={(event) => {
                  const nextEnabled = event.target.checked
                  updateConfig((current) => ({
                    ...current,
                    chairman_enabled: nextEnabled,
                    chairman_model: nextEnabled ? current.chairman_model : null,
                  }))
                }}
              />
              <span className="slider" />
            </label>
          </div>
          <p className="panel-copy">
            When enabled, the chairman gets only anonymized council positions and returns a synthesized answer.
          </p>
          {config.chairman_enabled ? (
            <ModelPicker
              eyebrow="Chairman selection"
              title="Chairman model"
              description="Choose the single model that will synthesize anonymized council positions."
              models={availableModels}
              selectedModels={config.chairman_model ? [config.chairman_model] : []}
              onToggle={toggleChairmanModel}
              maxSelection={1}
              mode="single"
              emptyMessage="No chairman model pinned yet."
              dropdownLabel="OpenRouter chairman catalog"
              withPanel={false}
              loading={catalogLoading}
              onOpen={ensureCatalogLoaded}
            />
          ) : (
            <div className="empty-state">Chairman disabled. The council will return one anonymized section per model.</div>
          )}
        </section>

        <section className="panel panel-stack runtime-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Runtime</p>
              <h2>Timeout and save</h2>
            </div>
            <span className="timeout-pill">{formatTimeout(config.council_timeout_ms)}</span>
          </div>
          <label className="field-label" htmlFor="timeout">Per-model timeout</label>
          <input
            id="timeout"
            type="range"
            min="10000"
            max="120000"
            step="5000"
            value={config.council_timeout_ms}
            onChange={(event) => updateConfig((current) => ({ ...current, council_timeout_ms: Number(event.target.value) }))}
          />
          <div className="helper-row">
            <span>10s</span>
            <span>120s</span>
          </div>
          {config.council_models.length < 2 ? (
            <p className="inline-note">Select at least two council models before saving.</p>
          ) : null}
          {isConfigDirty ? <p className="inline-note">You have unsaved council changes.</p> : null}
          <div className="control-row">
            <button type="button" className="button primary" onClick={handleSave} disabled={busy || !canSave}>
              Save &amp; Restart Server
            </button>
            <button type="button" className="button danger" onClick={handleStopServer} disabled={busy}>
              Stop Server
            </button>
          </div>
          {message ? <p className="notice success">{message}</p> : null}
          {error ? <p className="notice error">{error}</p> : null}
        </section>

        <section className="panel panel-stack mcp-apps-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">MCP apps</p>
              <h2>Universal client setup</h2>
            </div>
            <span className="chip idle">Stdio transport</span>
          </div>
          <p className="panel-copy">
            Paste this single `mcpServers` block into Claude Desktop, VS Code MCP, Cherry Studio, Cursor, or any
            compatible stdio client. The project path is detected automatically by the running backend.
          </p>
          <p className="inline-note">
            The dashboard stores its own key in the system keychain, but external MCP apps still need their own
            `OPENROUTER_API_KEY` entry or shell environment.
          </p>
          <div className="snippet-grid">
            <CodeSnippetCard
              eyebrow="Universal"
              title="mcpServers JSON"
              description="Most MCP apps accept this object directly. If your client asks for a single server object, copy the value under `llm-council` only."
              note={mcpSnippetNote}
              code={mcpClient.snippet || defaultMcpClient.snippet}
              isCopied={copiedSnippetId === 'universal-mcp'}
              onCopy={() => handleCopySnippet('universal-mcp', mcpClient.snippet || defaultMcpClient.snippet)}
            />
          </div>
        </section>
      </section>
    </main>
  )
}
