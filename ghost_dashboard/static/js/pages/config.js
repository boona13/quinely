/** Configuration page — tabbed layout */

const t = (key, params) => window.GhostI18n?.t(key, params) ?? key;

export async function render(container) {
  const { GhostAPI: api, GhostUtils: u } = window;
  const data = await api.get('/api/config');
  const cfg = data.config;
  const defs = data.defaults;

  const toggle = (key, label, desc) => {
    const on = cfg[key];
    const displayLabel = label || key.replace(/^enable_/, '').replace(/_/g, ' ');
    return `<div class="flex items-center justify-between py-2">
      <div>
        <span class="text-sm text-zinc-300">${displayLabel}</span>
        ${desc ? `<div class="text-[10px] text-zinc-600 mt-0.5">${desc}</div>` : ''}
      </div>
      <div class="toggle ${on ? 'on' : ''}" data-toggle="${key}"><span class="toggle-dot"></span></div>
    </div>`;
  };

  const numInput = (key, label, min, max, step) => `
    <div>
      <label class="form-label">${label}</label>
      <input type="number" class="form-input w-full" data-key="${key}" value="${cfg[key] ?? defs[key]}" min="${min}" max="${max}" step="${step || 1}">
    </div>`;

  const tabs = [
    { id: 'general',  label: t('config.general'),  icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z' },
    { id: 'features', label: t('config.features'), icon: 'M13 10V3L4 14h7v7l9-11h-7z' },
    { id: 'voice',    label: t('config.voice'),    icon: 'M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z' },
    { id: 'growth',   label: t('config.growth'),   icon: 'M13 7h8m0 0v8m0-8l-8 8-4-4-6 6' },
    { id: 'security', label: t('config.security'), icon: 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z' },
    { id: 'models',   label: t('config.modelsTab'),   icon: 'M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z' },
    { id: 'cloud',    label: 'Cloud Providers',      icon: 'M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.096A4.001 4.001 0 003 15z' },
  ];

  container.innerHTML = `
    <h1 class="page-header">${t('config.title')}</h1>
    <p class="page-desc">${t('config.subtitle')}</p>

    <div class="cfg-tabs">
      ${tabs.map((tb, i) => `
        <button class="cfg-tab ${i === 0 ? 'active' : ''}" data-tab="${tb.id}">
          <svg class="inline-block w-3.5 h-3.5 mr-1 -mt-0.5 opacity-60" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="${tb.icon}"/></svg>
          ${tb.label}
        </button>
      `).join('')}
    </div>

    <!-- ── General ──────────────────────────────────────────── -->
    <div class="cfg-tab-panel active" data-panel="general">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('config.timingLimits')}</h3>
          <div class="grid grid-cols-2 gap-3">
            ${numInput('poll_interval', t('config.pollInterval'), 0.1, 10, 0.1)}
            ${numInput('min_length', t('config.minTextLength'), 1, 200, 1)}
            ${numInput('rate_limit_seconds', t('config.rateLimit'), 0, 30, 1)}
            ${numInput('max_input_chars', t('config.maxInputChars'), 500, 50000, 500)}
            ${numInput('max_feed_items', t('config.maxFeedItems'), 10, 500, 10)}
            ${numInput('tool_loop_max_steps', t('config.maxToolSteps'), 1, 500, 10)}
          </div>
        </div>
        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('status.currentModel')}</h3>
          <div class="font-mono text-sm text-ghost-400">${u.escapeHtml(cfg.model || defs.model)}</div>
          <div class="text-[10px] text-zinc-600 mt-1">${t('config.changeOnModelsPage')}</div>
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mt-4">
        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('config.webFetch')}</h3>
          <div class="grid grid-cols-2 gap-3">
            ${numInput('web_fetch_max_chars', t('config.maxChars'), 1000, 200000, 1000)}
            ${numInput('web_fetch_timeout_seconds', t('config.timeout'), 5, 120, 5)}
          </div>
        </div>
        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('config.processLimits')}</h3>
          <div class="grid grid-cols-2 gap-3">
            ${numInput('max_shell_sessions', t('config.shellSessions'), 1, 20, 1)}
            ${numInput('max_background_processes', t('config.backgroundProcs'), 1, 50, 1)}
            ${numInput('dashboard_port', t('config.dashboardPort'), 1024, 65535, 1)}
          </div>
          <div class="text-[10px] text-zinc-600 mt-2">${t('config.dashboardPortNote')}</div>
        </div>
      </div>
    </div>

    <!-- ── Features ─────────────────────────────────────────── -->
    <div class="cfg-tab-panel" data-panel="features">
      <div class="stat-card">
        <h3 class="text-sm font-semibold text-white mb-3">${t('config.featureToggles')}</h3>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-x-8">
          ${['enable_tool_loop','enable_memory_db','enable_plugins','enable_skills','enable_system_tools','enable_browser_tools','enable_browser_use','enable_channels','enable_cron','enable_evolve','enable_future_features','enable_integrations','enable_mcp','enable_auto_retrieval','enable_web_search','enable_web_fetch','enable_image_gen','enable_vision','enable_tts','enable_canvas','enable_response_integrity','enable_security_audit','enable_session_memory'].map(k => toggle(k)).join('')}
        </div>
      </div>

      <!-- Session Maintenance -->
      <div class="stat-card mt-4">
        <h3 class="text-sm font-semibold text-white mb-1">${t('config.sessionMaintenance')}</h3>
        <div class="text-[10px] text-zinc-600 mb-3">${t('config.sessionMaintenanceDesc')}</div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="flex items-center justify-between py-2">
            <div>
              <span class="text-sm text-zinc-300">${t('config.autoCleanup')}</span>
              <div class="text-[10px] text-zinc-600 mt-0.5">${t('config.autoCleanupDesc')}</div>
            </div>
            <div class="toggle ${cfg.session_auto_cleanup !== false ? 'on' : ''}" data-toggle="session_auto_cleanup"><span class="toggle-dot"></span></div>
          </div>
          ${numInput('session_max_count', t('config.maxSessions'), 10, 10000, 10)}
          ${numInput('session_max_age_days', t('config.maxAgeDays'), 1, 365, 1)}
          ${numInput('session_disk_budget_mb', t('config.diskBudgetMb'), 50, 10000, 50)}
        </div>
      </div>

    </div>

    <!-- ── Voice ────────────────────────────────────────────── -->
    <div class="cfg-tab-panel" data-panel="voice">
      <div class="stat-card" id="voice-section">
        <div class="flex items-center justify-between py-2 border-b border-surface-600/30 mb-4">
          <div>
            <span class="text-sm text-zinc-300">${t('config.enableVoice')}</span>
            <div class="text-[10px] text-zinc-600 mt-0.5">${t('config.enableVoiceDesc')}</div>
          </div>
          <div class="toggle ${cfg.enable_voice !== false ? 'on' : ''}" data-toggle="enable_voice"><span class="toggle-dot"></span></div>
        </div>

        <div id="voice-controls" class="mb-4">
          <div class="flex items-center gap-3 mb-2">
            <span class="text-xs text-zinc-400">${t('common.status')}:</span>
            <span id="voice-state" class="text-xs text-zinc-500">${t('common.loading')}</span>
          </div>
          <div class="flex gap-2">
            <button id="btn-voice-wake" class="btn btn-sm" style="font-size:0.7rem">${t('config.startWake')}</button>
            <button id="btn-voice-talk" class="btn btn-sm" style="font-size:0.7rem">${t('config.startTalk')}</button>
            <button id="btn-voice-stop" class="btn btn-sm btn-danger hidden" style="font-size:0.7rem">${t('common.stop')}</button>
          </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label class="form-label">${t('config.wakeWords')}</label>
            <input type="text" class="form-input w-full text-xs" id="cfg-voice-wake-words" value="${(cfg.voice_wake_words || ['ghost','hey ghost']).join(', ')}" placeholder="${t('config.wakeWordsPlaceholder')}">
            <div class="text-[10px] text-zinc-600 mt-1">${t('config.wakeWordsDesc')}</div>
          </div>
          <div>
            <label class="form-label">${t('config.sttProvider')}</label>
            <select class="form-input w-full text-xs" id="cfg-voice-stt">
              <option value="auto" ${(cfg.voice_stt_provider||'auto')==='auto'?'selected':''}>${t('config.sttAuto')}</option>
              <option value="moonshine" ${cfg.voice_stt_provider==='moonshine'?'selected':''}>${t('config.sttMoonshine')}</option>
              <option value="openrouter" ${cfg.voice_stt_provider==='openrouter'?'selected':''}>${t('config.sttOpenRouter')}</option>
              <option value="whisper" ${cfg.voice_stt_provider==='whisper'?'selected':''}>${t('config.sttWhisper')}</option>
              <option value="groq" ${cfg.voice_stt_provider==='groq'?'selected':''}>${t('config.sttGroq')}</option>
              <option value="vosk" ${cfg.voice_stt_provider==='vosk'?'selected':''}>${t('config.sttVosk')}</option>
            </select>
          </div>
          <div>
            <label class="form-label">${t('config.silenceThreshold')}</label>
            <input type="number" class="form-input w-full text-xs" data-key="voice_silence_threshold" value="${cfg.voice_silence_threshold ?? 0.02}" min="0.001" max="1" step="0.005">
            <div class="text-[10px] text-zinc-600 mt-1">${t('config.silenceThresholdDesc')}</div>
          </div>
          <div>
            <label class="form-label">${t('config.silenceDuration')}</label>
            <input type="number" class="form-input w-full text-xs" data-key="voice_silence_duration" value="${cfg.voice_silence_duration ?? 2.0}" min="0.5" max="10" step="0.5">
            <div class="text-[10px] text-zinc-600 mt-1">${t('config.silenceDurationDesc')}</div>
          </div>
        </div>
        <label class="flex items-center gap-2 cursor-pointer mt-4">
          <input id="cfg-voice-chime" type="checkbox" ${cfg.voice_chime !== false ? 'checked' : ''}
            class="w-3.5 h-3.5 rounded bg-surface-700 border-surface-600 text-ghost-500">
          <span class="text-xs text-zinc-400">${t('config.chimeOnWake')}</span>
        </label>
      </div>
    </div>

    <!-- ── Growth ───────────────────────────────────────────── -->
    <div class="cfg-tab-panel" data-panel="growth">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('evolve.title')}</h3>
          <div class="flex items-center justify-between py-2 border-b border-surface-600/30 mb-3">
            <div>
              <span class="text-sm text-zinc-300">${t('config.autoApproveEvo')}</span>
              <div class="text-[10px] text-zinc-600 mt-0.5">${t('config.autoApproveEvoDesc')}</div>
            </div>
            <div class="toggle ${cfg.evolve_auto_approve ? 'on' : ''}" data-toggle="evolve_auto_approve"><span class="toggle-dot"></span></div>
          </div>
          <div class="flex items-center justify-between py-2">
            <div>
              <span class="text-sm text-zinc-300">${t('config.maxEvoPerHour')}</span>
              <div class="text-[10px] text-zinc-600 mt-0.5">${t('config.maxEvoPerHourDesc')}</div>
            </div>
            <input type="number" min="1" max="100" class="bg-surface-700 text-white text-sm rounded px-2 py-1 w-20 text-right" data-key="max_evolutions_per_hour" value="${cfg.max_evolutions_per_hour ?? 25}">
          </div>
        </div>

        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('config.growth')}</h3>
          <div class="flex items-center justify-between py-2 border-b border-surface-600/30 mb-3">
            <div>
              <span class="text-sm text-zinc-300">${t('config.enableGrowth')}</span>
              <div class="text-[10px] text-zinc-600 mt-0.5">${t('config.enableGrowthDesc')}</div>
            </div>
            <div class="toggle ${cfg.enable_growth !== false ? 'on' : ''}" data-toggle="enable_growth"><span class="toggle-dot"></span></div>
          </div>
          <div class="space-y-2" id="growth-schedules-container"></div>
          <div class="text-[10px] text-zinc-600 mt-2">${t('config.cronExpressions')}</div>
        </div>
      </div>
    </div>

    <!-- ── Security ─────────────────────────────────────────── -->
    <div class="cfg-tab-panel" data-panel="security">

      <div class="cfg-crosslink">
        <span>This tab covers <strong>channel access</strong> (who can DM Quinely). Firewall posture, threats and the full audit log live on the dedicated page.</span>
        <a href="#security_hub">Open Security &rarr;</a>
      </div>

      <!-- Channel Security / Allowlist -->
      <div class="stat-card mb-6">
        <h3 class="text-sm font-semibold text-white mb-1">${t('config.channelSecurity')}</h3>
        <div class="text-[10px] text-zinc-600 mb-4">${t('config.channelSecurityDesc')}</div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <div class="flex items-center justify-between py-2 border-b border-surface-600/30 mb-3">
              <div>
                <span class="text-sm text-zinc-300">${t('config.inboundEnabled')}</span>
                <div class="text-[10px] text-zinc-600 mt-0.5">${t('config.inboundEnabledDesc')}</div>
              </div>
              <div class="toggle ${cfg.channel_inbound_enabled !== false ? 'on' : ''}" data-toggle="channel_inbound_enabled"><span class="toggle-dot"></span></div>
            </div>
            <label class="form-label">${t('config.dmPolicy')}</label>
            <select class="form-input w-full text-xs" id="cfg-dm-policy">
              <option value="open" ${(cfg.channel_dm_policy||'open')==='open'?'selected':''}>${t('config.dmPolicyOpen')}</option>
              <option value="allowlist" ${cfg.channel_dm_policy==='allowlist'?'selected':''}>${t('config.dmPolicyAllowlist')}</option>
              <option value="blocklist" ${cfg.channel_dm_policy==='blocklist'?'selected':''}>${t('config.dmPolicyBlocklist')}</option>
              <option value="block" ${cfg.channel_dm_policy==='block'?'selected':''}>${t('config.dmPolicyBlock')}</option>
            </select>
            <div class="text-[10px] text-zinc-600 mt-1">${t('config.dmPolicyDesc')}</div>
          </div>

          <div>
            <label class="form-label">${t('config.allowedSenders')}</label>
            <div class="text-[10px] text-zinc-600 mb-2">${t('config.allowedSendersDesc')}</div>
            <div id="allowlist-senders" class="space-y-1 mb-2 max-h-40 overflow-y-auto"></div>
            <div class="flex gap-2">
              <input type="text" class="form-input text-xs flex-1 font-mono" id="new-sender-id" placeholder="${t('config.addSenderPlaceholder')}">
              <button id="btn-add-sender" class="btn btn-sm btn-primary">${t('config.addSender')}</button>
            </div>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('config.allowedCommands')}</h3>
          <textarea id="allowed-commands" class="form-input w-full h-40 font-mono text-xs">${(cfg.allowed_commands || []).join(', ')}</textarea>
          <div class="text-[10px] text-zinc-600 mt-1">${t('config.allowedCommandsDesc')}</div>
        </div>

        <div class="stat-card">
          <h3 class="text-sm font-semibold text-white mb-3">${t('config.allowedRoots')}</h3>
          <textarea id="allowed-roots" class="form-input w-full h-40 font-mono text-xs">${(cfg.allowed_roots || []).join('\n')}</textarea>
          <div class="text-[10px] text-zinc-600 mt-1">${t('config.allowedRootsDesc')}</div>
        </div>

        <div class="stat-card md:col-span-2">
          <div class="flex items-center justify-between mb-4">
            <h3 class="text-sm font-semibold text-white">${t('config.dangerousPolicy')}</h3>
            <div class="flex items-center gap-2">
              <span class="text-xs text-zinc-400">${t('config.enableDangerousInterpreters')}</span>
              <div class="toggle ${cfg.enable_dangerous_interpreters ? 'on' : ''}" data-toggle="enable_dangerous_interpreters" id="toggle-dangerous-interpreters"><span class="toggle-dot"></span></div>
            </div>
          </div>
          <div class="text-[10px] text-zinc-600 mb-4">${t('config.dangerousPolicyDesc')}</div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-6" id="dangerous-policy-container" style="${cfg.enable_dangerous_interpreters ? '' : 'opacity: 0.5; pointer-events: none;'}">
            <!-- Python Policy -->
            <div class="bg-surface-700/30 rounded p-3">
              <h4 class="text-xs font-medium text-ghost-400 mb-3 flex items-center gap-2">
                <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"/></svg>
                ${t('config.python')}
              </h4>
              <div class="space-y-3">
                <div class="flex items-center justify-between">
                  <span class="text-xs text-zinc-400">${t('config.allowPython')}</span>
                  <div class="toggle ${(cfg.dangerous_command_policy?.python?.allow !== false) ? 'on' : ''}" data-toggle="python_allow"><span class="toggle-dot"></span></div>
                </div>
                <div class="flex items-center justify-between">
                  <span class="text-xs text-zinc-400">${t('config.requireWorkspace')}</span>
                  <div class="toggle ${cfg.dangerous_command_policy?.python?.require_workspace ? 'on' : ''}" data-toggle="python_require_workspace"><span class="toggle-dot"></span></div>
                </div>
                <div>
                  <label class="text-xs text-zinc-500 block mb-1">${t('config.denyFlags')}</label>
                  <input type="text" class="form-input w-full text-xs font-mono" id="python-deny-flags" value="${(cfg.dangerous_command_policy?.python?.deny_flags || []).join(', ')}" placeholder="-c, -m, exec, eval, compile, __import__, os.system, subprocess, pty">
                </div>
              </div>
            </div>

            <!-- Pip Policy -->
            <div class="bg-surface-700/30 rounded p-3">
              <h4 class="text-xs font-medium text-amber-400 mb-3 flex items-center gap-2">
                <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
                ${t('config.pip')}
              </h4>
              <div class="space-y-3">
                <div class="flex items-center justify-between">
                  <span class="text-xs text-zinc-400">${t('config.allowPip')}</span>
                  <div class="toggle ${(cfg.dangerous_command_policy?.pip?.allow !== false) ? 'on' : ''}" data-toggle="pip_allow"><span class="toggle-dot"></span></div>
                </div>
                <div class="flex items-center justify-between">
                  <span class="text-xs text-zinc-400">${t('config.requireWorkspace')}</span>
                  <div class="toggle ${cfg.dangerous_command_policy?.pip?.require_workspace ? 'on' : ''}" data-toggle="pip_require_workspace"><span class="toggle-dot"></span></div>
                </div>
                <div>
                  <label class="text-xs text-zinc-500 block mb-1">${t('config.allowSubcommands')}</label>
                  <input type="text" class="form-input w-full text-xs font-mono" id="pip-allow-subcommands" value="${(cfg.dangerous_command_policy?.pip?.allow_subcommands || ['install', 'show', 'freeze', 'list', 'index']).join(', ')}" placeholder="install, show, freeze, list, index">
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Dashboard Authentication -->
        <div class="stat-card md:col-span-2">
          <h3 class="text-sm font-semibold text-white mb-1">Dashboard Access Token</h3>
          <div class="text-[10px] text-zinc-600 mb-3">Optional. When set, the dashboard requires this token to log in (or the <span class="font-mono">GHOST_DASHBOARD_TOKEN</span> env var). Leave blank to keep the dashboard open on localhost.</div>
          <input type="password" data-key="dashboard_auth_token" value="${cfg.dashboard_auth_token || ''}" class="form-input w-full" placeholder="Leave blank for no auth" autocomplete="new-password">
        </div>

        <!-- Tool Registration Security -->
        <div class="stat-card md:col-span-2">
          <div class="flex items-center justify-between mb-2">
            <h3 class="text-sm font-semibold text-white">${t('config.toolRegSecurity')}</h3>
            <div class="flex items-center gap-2">
              <span class="text-xs text-zinc-400">${t('config.strictToolReg')}</span>
              <div class="toggle ${cfg.strict_tool_registration !== false ? 'on' : ''}" data-toggle="strict_tool_registration"><span class="toggle-dot"></span></div>
            </div>
          </div>
          <div class="text-[10px] text-zinc-600">${t('config.strictToolRegDesc')}</div>
        </div>
      </div>
    </div>

    <!-- ── Models ───────────────────────────────────────────── -->
    <div class="cfg-tab-panel" data-panel="models">
      <div class="cfg-crosslink">
        <span>This tab manages your <strong>Hugging Face login</strong> for local/open models. To browse and switch the active model, use the Models page.</span>
        <a href="#models">Open Models &rarr;</a>
      </div>
      <div class="stat-card mb-4">
        <h3 class="text-sm font-semibold text-white mb-1">${t('config.hfTokenTitle')}</h3>
        <div class="text-[10px] text-zinc-600 mb-3">${t('config.hfTokenDesc')}</div>

        <!-- Main login area -->
        <div class="flex items-center justify-between mb-3">
          <span class="text-[10px] px-2 py-0.5 rounded-full ${cfg.hf_token ? 'bg-green-900/40 text-green-400' : 'bg-zinc-800 text-zinc-500'}">${cfg.hf_token ? t('config.hfConnected') : t('config.hfNotConnected')}</span>
        </div>

        <div class="flex items-center gap-3 mb-2">
          <button id="btn-hf-oauth" class="btn h-10 bg-yellow-600 hover:bg-yellow-500 text-black font-semibold px-6 text-sm">
            <svg class="w-4 h-4 mr-2 inline-block -mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"/></svg>
            ${cfg.hf_token ? t('config.hfReconnectBtn') : t('config.hfLoginBtn')}
          </button>
          <span id="hf-oauth-status" class="text-xs"></span>
        </div>

        <!-- Device flow UI (hidden by default, shown during auth) -->
        <div id="hf-device-flow" class="hidden mt-3 p-4 rounded-lg bg-surface-900/80 border border-ghost-700/30">
          <div class="text-center">
            <div class="text-xs text-zinc-400 mb-2">${t('config.hfDeviceStep1')}</div>
            <a id="hf-device-link" href="https://huggingface.co/device" target="_blank" rel="noopener" class="text-base text-ghost-400 hover:text-ghost-300 underline font-mono">huggingface.co/device</a>
            <div class="text-xs text-zinc-400 mt-4 mb-1">${t('config.hfDeviceStep2')}</div>
            <div id="hf-device-code" class="text-3xl font-bold font-mono text-white tracking-[0.3em] my-2 select-all">----</div>
            <button id="btn-hf-copy-code" class="text-xs text-ghost-400 hover:text-ghost-300 underline mt-1">${t('config.hfCopyCode')}</button>
            <div class="mt-4">
              <div class="flex items-center justify-center gap-2">
                <div class="w-3.5 h-3.5 border-2 border-ghost-400 border-t-transparent rounded-full animate-spin"></div>
                <span class="text-xs text-zinc-400">${t('config.hfWaiting')}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Hidden client ID (for advanced override only) -->
        <input type="hidden" id="cfg-hf-client-id" value="${cfg.hf_oauth_client_id || ''}">

        <!-- Manual token (collapsible alternative) -->
        <details class="group mt-3">
          <summary class="text-[10px] text-zinc-500 cursor-pointer hover:text-zinc-400 mb-2 select-none">
            ${t('config.hfManualToken')} ▸
          </summary>
          <div class="flex gap-2 items-end">
            <div class="flex-1">
              <label class="form-label">${t('config.hfToken')}</label>
              <input type="password" class="form-input w-full text-xs font-mono" id="cfg-hf-token" value="${cfg.hf_token || ''}" placeholder="hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
            </div>
            <button id="btn-hf-test" class="btn btn-sm btn-primary h-8">${t('config.hfTestBtn')}</button>
          </div>
          <div id="hf-token-status" class="text-[10px] mt-2"></div>
          <div class="text-[10px] text-zinc-600 mt-1">${t('config.hfTokenHelp')}</div>
        </details>
      </div>
      <div class="stat-card mb-4">
        <h3 class="text-sm font-semibold text-white mb-1">${t('config.anthropicSettings')}</h3>
        <div class="text-[10px] text-zinc-600 mb-3">${t('config.anthropicOnlyNote')}</div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label class="form-label">${t('config.reasoningEffort')}</label>
            <select class="form-input w-full text-xs" id="cfg-anthropic-effort">
              <option value="low" ${(cfg.anthropic_effort||'high')==='low'?'selected':''}>${t('config.effortLow')}</option>
              <option value="medium" ${(cfg.anthropic_effort||'high')==='medium'?'selected':''}>${t('config.effortMedium')}</option>
              <option value="high" ${(cfg.anthropic_effort||'high')==='high'?'selected':''}>${t('config.effortHigh')}</option>
            </select>
            <div class="text-[10px] text-zinc-600 mt-1">${t('config.effortDesc')}</div>
          </div>
          <div>
            <div class="flex items-center justify-between py-2">
              <div>
                <span class="text-sm text-zinc-300">${t('config.contextCompaction')}</span>
                <div class="text-[10px] text-zinc-600 mt-0.5">${t('config.autoCompress')}</div>
              </div>
              <div class="toggle ${cfg.anthropic_context_compaction ? 'on' : ''}" data-toggle="anthropic_context_compaction"><span class="toggle-dot"></span></div>
            </div>
          </div>
          <div>
            <label class="form-label">${t('config.compactionRatio')}</label>
            <input type="number" class="form-input w-full text-xs" data-key="anthropic_context_compaction_ratio" value="${cfg.anthropic_context_compaction_ratio ?? 0.5}" min="0" max="1" step="0.05">
            <div class="text-[10px] text-zinc-600 mt-1">${t('config.compactionRatioDesc')}</div>
          </div>
        </div>
      </div>
      <div class="stat-card mb-4">
        <h3 class="text-sm font-semibold text-white mb-1">${t('config.skillModelAliases')}</h3>
        <div class="text-[10px] text-zinc-600 mb-3">${t('config.skillModelAliasesDesc')}</div>
        <div id="skill-model-aliases-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3"></div>
        <div class="mt-3 pt-3 border-t border-surface-600/30">
          <div class="flex gap-2">
            <input type="text" id="new-alias-name" class="form-input text-xs w-32" placeholder="${t('config.aliasName')}">
            <input type="text" id="new-alias-model" class="form-input text-xs flex-1 font-mono" placeholder="${t('config.providerModelId')}">
            <button id="btn-add-alias" class="btn btn-sm btn-primary">${t('common.add')}</button>
          </div>
        </div>
      </div>
      <div class="stat-card">
        <h3 class="text-sm font-semibold text-white mb-1">${t('config.toolModels')}</h3>
        <div class="text-[10px] text-zinc-600 mb-3">${t('config.toolModelsDesc')}</div>
        <div id="tool-models-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3"></div>
      </div>

      <div class="stat-card mt-4">
        <h3 class="text-sm font-semibold text-white mb-1">${t('config.providerFallbackChains')}</h3>
        <div class="text-[10px] text-zinc-600 mb-3">${t('config.providerFallbackChainsDesc')}</div>
        <div id="provider-chains-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"></div>
      </div>
    </div>

    <!-- ── Cloud Providers ────────────────────────────────────── -->
    <div class="cfg-tab-panel" data-panel="cloud">
      <div class="stat-card mb-4">
        <h3 class="text-sm font-semibold text-white mb-1">Cloud Video Providers</h3>
        <div class="text-[10px] text-zinc-600 mb-3">Configure paid cloud APIs for high-quality video generation. Each provider requires an API key and has per-generation costs.</div>
      </div>
      <div id="cloud-providers-container" class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="stat-card animate-pulse"><div class="h-32 bg-surface-800 rounded"></div></div>
      </div>
      <div id="cloud-costs-card" class="stat-card mt-4">
        <h3 class="text-sm font-semibold text-white mb-2">Monthly Costs</h3>
        <div id="cloud-costs-summary" class="text-xs text-zinc-400">Loading...</div>
      </div>
    </div>

    <!-- ── Save bar ─────────────────────────────────────────── -->
    <div class="flex gap-3 mt-6">
      <button id="btn-save-config" class="btn btn-primary">${t('config.saveConfig')}</button>
      <button id="btn-reset-config" class="btn btn-danger btn-sm">${t('config.resetDefaults')}</button>
    </div>

    <!-- ── Factory Reset ─────────────────────────────────────── -->
    <div class="stat-card mt-8 border border-red-900/30">
      <h3 class="text-sm font-semibold text-red-400 mb-1">Reset Quinely</h3>
      <div class="text-[10px] text-zinc-600 mb-4">Wipe Quinely's runtime data in ~/.ghost/ and start fresh. A timestamped backup is always created before any reset.</div>
      <div class="flex flex-wrap gap-3">
        <button id="btn-reset-memory" class="btn btn-sm" style="border:1px solid rgba(239,68,68,0.3); color:#f87171;">
          Clear Memory
        </button>
        <button id="btn-reset-creds" class="btn btn-sm" style="border:1px solid rgba(239,68,68,0.3); color:#f87171;">
          Reset Config &amp; Credentials
        </button>
        <button id="btn-reset-all" class="btn btn-sm btn-danger">
          Full Factory Reset
        </button>
      </div>
      <div id="reset-status" class="text-xs mt-3"></div>
    </div>
  `;

  // ── Tab switching ────────────────────────────────────────────
  container.querySelectorAll('.cfg-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('.cfg-tab').forEach(tb => tb.classList.remove('active'));
      container.querySelectorAll('.cfg-tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = container.querySelector(`[data-panel="${btn.dataset.tab}"]`);
      if (panel) panel.classList.add('active');
    });
  });

  // ── Growth schedules ─────────────────────────────────────────
  const routines = [
    {id: 'tech_scout', label: t('config.routineTechScout'), desc: t('config.routineTechScoutDesc')},
    {id: 'health_check', label: t('config.routineHealthCheck'), desc: t('config.routineHealthCheckDesc')},
    {id: 'user_context', label: t('config.routineUserContext'), desc: t('config.routineUserContextDesc')},
    {id: 'skill_improver', label: t('config.routineSkillImprover'), desc: t('config.routineSkillImproverDesc')},
    {id: 'soul_evolver', label: t('config.routineSoulEvolver'), desc: t('config.routineSoulEvolverDesc')},
    {id: 'bug_hunter', label: t('config.routineBugHunter'), desc: t('config.routineBugHunterDesc')},
    {id: 'competitive_intel', label: t('config.routineCompetitiveIntel'), desc: t('config.routineCompetitiveIntelDesc')},
    {id: 'content_health', label: t('config.routineContentHealth'), desc: t('config.routineContentHealthDesc')},
    {id: 'security_patrol', label: t('config.routineSecurityPatrol'), desc: t('config.routineSecurityPatrolDesc')},
    {id: 'visual_monitor', label: t('config.routineVisualMonitor'), desc: t('config.routineVisualMonitorDesc')},
  ];
  const defaultScheds = {tech_scout:'0 */12 * * *',health_check:'0 */2 * * *',user_context:'0 */4 * * *',skill_improver:'0 3 * * *',soul_evolver:'0 4 * * 0',bug_hunter:'0 */6 * * *',competitive_intel:'0 6 * * 1,4',content_health:'0 4 * * 0',security_patrol:'0 5 * * *',visual_monitor:'0 */8 * * *'};
  const schedContainer = container.querySelector('#growth-schedules-container');
  if (schedContainer) {
    schedContainer.innerHTML = routines.map(r => {
      const val = (cfg.growth_schedules || {})[r.id] || defaultScheds[r.id] || '';
      return '<div class="flex items-center gap-2 py-1">' +
        '<div class="flex-1 min-w-0">' +
        '<div class="text-xs text-zinc-300">' + u.escapeHtml(r.label) + '</div>' +
        '<div class="text-[10px] text-zinc-600">' + u.escapeHtml(r.desc) + '</div>' +
        '</div>' +
        '<input type="text" class="form-input text-xs w-28 font-mono growth-schedule" data-routine="' + r.id + '" value="' + u.escapeHtml(val) + '" placeholder="cron expr">' +
        '</div>';
    }).join('');
  }

  // ── Tool models ──────────────────────────────────────────────
  const toolModelDefs = [
    {key: 'image_gen_openrouter', label: t('config.toolModelImageGenOR'), def: 'google/gemini-3-pro-image-preview'},
    {key: 'image_gen_gemini', label: t('config.toolModelImageGenGemini'), def: 'gemini-3-pro-image-preview'},
    {key: 'image_gen_openai', label: t('config.toolModelImageGenOpenAI'), def: 'gpt-image-1'},
    {key: 'vision_openai', label: t('config.toolModelVisionOpenAI'), def: 'gpt-4o'},
    {key: 'vision_openrouter', label: t('config.toolModelVisionOR'), def: 'openai/gpt-4o'},
    {key: 'vision_gemini', label: t('config.toolModelVisionGemini'), def: 'gemini-2.5-flash'},
    {key: 'vision_anthropic', label: t('config.toolModelVisionAnthropic'), def: 'claude-sonnet-4-20250514'},
    {key: 'vision_ollama', label: t('config.toolModelVisionOllama'), def: 'llava'},
    {key: 'web_search_perplexity', label: t('config.toolModelSearchPerplexityOR'), def: 'perplexity/sonar-pro'},
    {key: 'web_search_perplexity_direct', label: t('config.toolModelSearchPerplexityDirect'), def: 'sonar-pro'},
    {key: 'web_search_grok', label: t('config.toolModelSearchGrok'), def: 'grok-3-fast'},
    {key: 'web_search_openai', label: t('config.toolModelSearchOpenAI'), def: 'gpt-4.1-mini'},
    {key: 'web_search_gemini', label: t('config.toolModelSearchGemini'), def: 'gemini-2.5-flash'},
    {key: 'grok_openrouter', label: t('config.toolModelGrokOR'), def: 'x-ai/grok-4-fast'},
    {key: 'tts_openai', label: t('config.toolModelTtsOpenAI'), def: 'tts-1'},
    {key: 'tts_elevenlabs', label: t('config.toolModelTtsElevenLabs'), def: 'eleven_multilingual_v2'},
    {key: 'embedding_openrouter', label: t('config.toolModelEmbeddingOR'), def: 'openai/text-embedding-3-small'},
    {key: 'embedding_gemini', label: t('config.toolModelEmbeddingGemini'), def: 'text-embedding-004'},
    {key: 'embedding_ollama', label: t('config.toolModelEmbeddingOllama'), def: 'nomic-embed-text'},
    {key: 'vision_deepseek', label: t('config.toolModelVisionDeepSeek'), def: 'deepseek-chat'},
  ];
  const tmContainer = container.querySelector('#tool-models-container');
  if (tmContainer) {
    const saved = cfg.tool_models || {};
    tmContainer.innerHTML = toolModelDefs.map(m => {
      const val = saved[m.key] || '';
      return '<div>' +
        '<label class="text-[11px] text-zinc-400 block mb-0.5">' + u.escapeHtml(m.label) + '</label>' +
        '<input type="text" class="form-input w-full text-xs font-mono tool-model-input" ' +
          'data-tm-key="' + m.key + '" ' +
          'value="' + u.escapeHtml(val) + '" ' +
          'placeholder="' + u.escapeHtml(m.def) + '">' +
        '</div>';
    }).join('');
  }

  // ── Skill Model Aliases ──────────────────────────────────────
  const aliasContainer = container.querySelector('#skill-model-aliases-container');
  const aliasNameInput = container.querySelector('#new-alias-name');
  const aliasModelInput = container.querySelector('#new-alias-model');
  const addAliasBtn = container.querySelector('#btn-add-alias');
  let currentAliases = { ...(cfg.skill_model_aliases || {}) };

  function renderAliases() {
    if (!aliasContainer) return;
    const entries = Object.entries(currentAliases);
    if (entries.length === 0) {
      aliasContainer.innerHTML = '<div class="text-[11px] text-zinc-600 col-span-full">' + t('config.noAliases') + '</div>';
      return;
    }
    aliasContainer.innerHTML = entries.map(([name, model]) => {
      return '<div class="flex items-center gap-2 bg-surface-700/50 rounded px-2 py-1.5">' +
        '<span class="text-xs text-ghost-400 font-medium">' + u.escapeHtml(name) + '</span>' +
        '<span class="text-[10px] text-zinc-500 flex-1 truncate font-mono">' + u.escapeHtml(model) + '</span>' +
        '<button class="btn btn-ghost btn-sm text-zinc-500 hover:text-red-400 remove-alias" data-alias="' + u.escapeHtml(name) + '" title="' + t('common.remove') + '">×</button>' +
        '</div>';
    }).join('');
    aliasContainer.querySelectorAll('.remove-alias').forEach(btn => {
      btn.addEventListener('click', () => {
        const aliasName = btn.dataset.alias;
        delete currentAliases[aliasName];
        renderAliases();
      });
    });
  }

  renderAliases();

  if (addAliasBtn) {
    addAliasBtn.addEventListener('click', () => {
      const name = aliasNameInput?.value.trim();
      const model = aliasModelInput?.value.trim();
      if (!name || !model) {
        u.toast(t('config.aliasRequiredFields'), 'error');
        return;
      }
      if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
        u.toast(t('config.aliasNameFormat'), 'error');
        return;
      }
      currentAliases[name] = model;
      if (aliasNameInput) aliasNameInput.value = '';
      if (aliasModelInput) aliasModelInput.value = '';
      renderAliases();
    });
  }

  // ── Provider Fallback Chains (drag-to-reorder) ───────────────
  const CHAIN_DEFS = {
    web_search: {
      label: t('config.chainWebSearch'),
      desc: t('config.chainWebSearchDesc'),
      providers: {
        perplexity_openrouter: 'Perplexity (OpenRouter)',
        perplexity_direct: 'Perplexity (direct)',
        grok: 'Grok / xAI',
        openai: 'OpenAI',
        brave: 'Brave Search',
        gemini: 'Gemini (Google)',
      },
    },
    image_gen: {
      label: t('config.chainImageGen'),
      desc: t('config.chainImageGenDesc'),
      providers: {
        openrouter: 'OpenRouter',
        google: 'Google Gemini',
        openai: 'OpenAI (DALL-E)',
      },
    },
    vision: {
      label: t('config.chainVision'),
      desc: t('config.chainVisionDesc'),
      providers: {
        openai: 'OpenAI (GPT-4o)',
        openrouter: 'OpenRouter',
        google: 'Google Gemini',
        anthropic: 'Anthropic (Claude)',
        ollama: 'Ollama (local)',
      },
    },
    tts: {
      label: t('config.chainTts'),
      desc: t('config.chainTtsDesc'),
      providers: {
        edge: 'Edge TTS (free)',
        openai: 'OpenAI TTS',
        elevenlabs: 'ElevenLabs',
      },
    },
    embeddings: {
      label: t('config.chainEmbeddings'),
      desc: t('config.chainEmbeddingsDesc'),
      providers: {
        openrouter: 'OpenRouter',
        gemini: 'Google Gemini',
        ollama: 'Ollama (local)',
      },
    },
    voice_stt: {
      label: t('config.chainStt'),
      desc: t('config.chainSttDesc'),
      providers: {
        moonshine: 'Moonshine (on-device)',
        openrouter: 'OpenRouter (Whisper)',
        whisper: 'OpenAI Whisper',
        groq: 'Groq Whisper',
        vosk: 'Vosk (offline)',
      },
    },
  };

  const savedChains = cfg.provider_chains || defs.provider_chains || {};
  const currentChains = {};
  for (const [chainKey, def] of Object.entries(CHAIN_DEFS)) {
    const allIds = Object.keys(def.providers);
    const saved = savedChains[chainKey] || allIds;
    const enabled = saved.filter(id => allIds.includes(id));
    const disabled = allIds.filter(id => !enabled.includes(id));
    currentChains[chainKey] = { enabled, disabled };
  }

  function renderChain(chainKey) {
    const def = CHAIN_DEFS[chainKey];
    const state = currentChains[chainKey];
    const all = [...state.enabled, ...state.disabled];
    let html = '<div class="chain-card-header">' +
      '<h4>' + u.escapeHtml(def.label) + '<span style="font-weight:400;color:rgba(255,255,255,0.3);margin-inline-start:6px;font-size:10px">' + u.escapeHtml(def.desc) + '</span></h4>' +
      '<button class="chain-reset" data-chain-reset="' + chainKey + '">' + t('common.reset') + '</button>' +
      '</div><div class="chain-list" data-chain="' + chainKey + '">';
    let pos = 1;
    for (const id of all) {
      const isEnabled = state.enabled.includes(id);
      html += '<div class="chain-item' + (isEnabled ? '' : ' disabled') + '" draggable="true" data-provider="' + id + '">' +
        '<span class="grip">⠿</span>' +
        '<span class="pos">' + (isEnabled ? pos++ : '—') + '</span>' +
        '<span class="provider-name">' + u.escapeHtml(def.providers[id] || id) + '</span>' +
        '<div class="chain-toggle' + (isEnabled ? ' on' : '') + '"><span class="dot"></span></div>' +
        '</div>';
    }
    html += '</div>';
    return html;
  }

  const chainsContainer = container.querySelector('#provider-chains-container');
  if (chainsContainer) {
    chainsContainer.innerHTML = Object.keys(CHAIN_DEFS).map(k =>
      '<div class="bg-surface-700/30 rounded p-3" data-chain-card="' + k + '">' + renderChain(k) + '</div>'
    ).join('');

    function refreshChainCard(chainKey) {
      const card = chainsContainer.querySelector('[data-chain-card="' + chainKey + '"]');
      if (card) card.innerHTML = renderChain(chainKey);
      attachChainEvents(chainKey);
    }

    function attachChainEvents(chainKey) {
      const list = chainsContainer.querySelector('.chain-list[data-chain="' + chainKey + '"]');
      if (!list) return;
      let dragItem = null;

      list.querySelectorAll('.chain-item').forEach(item => {
        item.addEventListener('dragstart', e => {
          dragItem = item;
          item.classList.add('dragging');
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', item.dataset.provider);
        });
        item.addEventListener('dragend', () => {
          item.classList.remove('dragging');
          list.querySelectorAll('.chain-item').forEach(el => el.classList.remove('drag-over'));
          dragItem = null;
        });
        item.addEventListener('dragover', e => {
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          if (dragItem && item !== dragItem) {
            list.querySelectorAll('.chain-item').forEach(el => el.classList.remove('drag-over'));
            item.classList.add('drag-over');
          }
        });
        item.addEventListener('dragleave', () => { item.classList.remove('drag-over'); });
        item.addEventListener('drop', e => {
          e.preventDefault();
          if (!dragItem || item === dragItem) return;
          const items = [...list.querySelectorAll('.chain-item')];
          const fromIdx = items.indexOf(dragItem);
          const toIdx = items.indexOf(item);
          if (fromIdx < 0 || toIdx < 0) return;
          const state = currentChains[chainKey];
          const allIds = [...state.enabled, ...state.disabled];
          const [moved] = allIds.splice(fromIdx, 1);
          allIds.splice(toIdx, 0, moved);
          state.enabled = allIds.filter(id => !state.disabled.includes(id));
          state.disabled = allIds.filter(id => state.disabled.includes(id));
          refreshChainCard(chainKey);
        });

        const toggleEl = item.querySelector('.chain-toggle');
        if (toggleEl) {
          toggleEl.addEventListener('click', e => {
            e.stopPropagation();
            const id = item.dataset.provider;
            const state = currentChains[chainKey];
            if (state.enabled.includes(id)) {
              state.enabled = state.enabled.filter(x => x !== id);
              state.disabled.push(id);
            } else {
              state.disabled = state.disabled.filter(x => x !== id);
              state.enabled.push(id);
            }
            refreshChainCard(chainKey);
          });
        }
      });

      const resetBtn = chainsContainer.querySelector('[data-chain-reset="' + chainKey + '"]');
      if (resetBtn) {
        resetBtn.addEventListener('click', () => {
          const allIds = Object.keys(CHAIN_DEFS[chainKey].providers);
          const defaultOrder = (defs.provider_chains || {})[chainKey] || allIds;
          currentChains[chainKey] = {
            enabled: defaultOrder.filter(id => allIds.includes(id)),
            disabled: allIds.filter(id => !defaultOrder.includes(id)),
          };
          refreshChainCard(chainKey);
        });
      }
    }

    Object.keys(CHAIN_DEFS).forEach(k => attachChainEvents(k));
  }

  // ── Toggles ──────────────────────────────────────────────────
  container.querySelectorAll('.toggle').forEach(el => {
    el.addEventListener('click', () => {
      el.classList.toggle('on');
      if (el.dataset.toggle === 'enable_dangerous_interpreters') {
        const containerEl = document.getElementById('dangerous-policy-container');
        if (containerEl) {
          if (el.classList.contains('on')) {
            containerEl.style.opacity = '1';
            containerEl.style.pointerEvents = 'auto';
          } else {
            containerEl.style.opacity = '0.5';
            containerEl.style.pointerEvents = 'none';
          }
        }
      }
    });
  });

  // ── Channel Security Allowlist ────────────────────────────────
  const allowlistContainer = container.querySelector('#allowlist-senders');
  const newSenderInput = container.querySelector('#new-sender-id');
  const addSenderBtn = container.querySelector('#btn-add-sender');
  const dmPolicySelect = container.querySelector('#cfg-dm-policy');
  let currentAllowlist = [...(cfg.channel_allowed_senders || [])];

  function renderAllowlist() {
    if (!allowlistContainer) return;
    if (currentAllowlist.length === 0) {
      allowlistContainer.innerHTML = '<div class="text-[10px] text-zinc-600">' + t('config.noSendersYet') + '</div>';
      return;
    }
    allowlistContainer.innerHTML = currentAllowlist.map(sid => {
      return '<div class="flex items-center gap-2 bg-surface-700/50 rounded px-2 py-1">' +
        '<span class="text-xs text-zinc-300 font-mono flex-1">' + u.escapeHtml(sid) + '</span>' +
        '<button class="btn btn-ghost btn-sm text-zinc-500 hover:text-red-400 remove-sender" data-sender="' + u.escapeHtml(sid) + '" title="' + t('common.remove') + '">\u00d7</button>' +
        '</div>';
    }).join('');
    allowlistContainer.querySelectorAll('.remove-sender').forEach(btn => {
      btn.addEventListener('click', () => {
        currentAllowlist = currentAllowlist.filter(s => s !== btn.dataset.sender);
        renderAllowlist();
      });
    });
  }

  renderAllowlist();

  if (addSenderBtn) {
    addSenderBtn.addEventListener('click', () => {
      const sid = newSenderInput?.value.trim();
      if (!sid) { u.toast(t('config.senderRequired'), 'error'); return; }
      if (currentAllowlist.includes(sid)) { u.toast(t('config.senderExists'), 'error'); return; }
      currentAllowlist.push(sid);
      if (newSenderInput) newSenderInput.value = '';
      renderAllowlist();
    });
  }

  if (newSenderInput) {
    newSenderInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); addSenderBtn?.click(); }
    });
  }

  if (dmPolicySelect) {
    dmPolicySelect.addEventListener('change', () => {
      const panel = allowlistContainer?.closest('.grid');
      const senderCol = panel?.querySelector('#allowlist-senders')?.closest('div');
      if (senderCol) {
        const isAllowlist = dmPolicySelect.value === 'allowlist';
        senderCol.style.opacity = isAllowlist ? '1' : '0.4';
        senderCol.style.pointerEvents = isAllowlist ? 'auto' : 'none';
      }
    });
    dmPolicySelect.dispatchEvent(new Event('change'));
  }

  // ── Voice controls ───────────────────────────────────────────
  const voiceStateEl = container.querySelector('#voice-state');
  const wakeBtn = container.querySelector('#btn-voice-wake');
  const talkBtn = container.querySelector('#btn-voice-talk');
  const stopBtn2 = container.querySelector('#btn-voice-stop');

  async function refreshVoiceState() {
    try {
      const vs = await api.get('/api/voice/status');
      const active = vs.ok && vs.state !== 'idle' && vs.state !== 'unavailable';
      const labels = { wake_listening: t('config.wakeActive'), talk_listening: t('config.talkActive'), capturing: t('config.capturing'), processing: t('config.processingAudio'), speaking: t('config.speakingAudio') };
      voiceStateEl.textContent = active ? (labels[vs.state] || vs.state) : (vs.ok ? t('status.idle') : t('config.unavailable'));
      voiceStateEl.className = active ? 'text-xs text-emerald-400' : 'text-xs text-zinc-500';
      if (active) { wakeBtn.classList.add('hidden'); talkBtn.classList.add('hidden'); stopBtn2.classList.remove('hidden'); }
      else { wakeBtn.classList.remove('hidden'); talkBtn.classList.remove('hidden'); stopBtn2.classList.add('hidden'); }
    } catch { voiceStateEl.textContent = t('config.unavailable'); }
  }
  refreshVoiceState();

  wakeBtn?.addEventListener('click', async () => { await api.post('/api/voice/wake/start'); refreshVoiceState(); });
  talkBtn?.addEventListener('click', async () => { await api.post('/api/voice/talk/start'); refreshVoiceState(); });
  stopBtn2?.addEventListener('click', async () => { await api.post('/api/voice/wake/stop'); refreshVoiceState(); });

  // ── Save ─────────────────────────────────────────────────────
  document.getElementById('btn-save-config')?.addEventListener('click', async () => {
    const updates = {};
    container.querySelectorAll('[data-key]').forEach(inp => {
      const k = inp.dataset.key;
      updates[k] = inp.type === 'number' ? parseFloat(inp.value) : inp.value;
    });
    container.querySelectorAll('[data-toggle]').forEach(el => {
      updates[el.dataset.toggle] = el.classList.contains('on');
    });
    const cmds = document.getElementById('allowed-commands').value;
    updates.allowed_commands = cmds.split(',').map(s => s.trim()).filter(Boolean);
    const roots = document.getElementById('allowed-roots').value;
    updates.allowed_roots = roots.split('\n').map(s => s.trim()).filter(Boolean);

    const enableDangerous = container.querySelector('[data-toggle="enable_dangerous_interpreters"]')?.classList.contains('on') || false;
    updates.enable_dangerous_interpreters = enableDangerous;
    if (enableDangerous && !cfg.enable_dangerous_interpreters) {
      const token = prompt(t('config.dangerousConfirmPrompt'));
      if (token === 'I_UNDERSTAND_THE_RISK') {
        updates.dangerous_interpreters_confirmation = token;
      } else {
        u.toast(t('config.dangerousConfirmRequired'), 'error');
        return;
      }
    }
    const pythonAllowEl = container.querySelector('[data-toggle="python_allow"]');
    const pythonRequireWsEl = container.querySelector('[data-toggle="python_require_workspace"]');
    const pythonDenyFlagsEl = document.getElementById('python-deny-flags');
    const pipAllowEl = container.querySelector('[data-toggle="pip_allow"]');
    const pipRequireWsEl = container.querySelector('[data-toggle="pip_require_workspace"]');
    const pipAllowSubcommandsEl = document.getElementById('pip-allow-subcommands');

    updates.dangerous_command_policy = {
      python: {
        allow: pythonAllowEl ? pythonAllowEl.classList.contains('on') : true,
        require_workspace: pythonRequireWsEl ? pythonRequireWsEl.classList.contains('on') : false,
        deny_flags: pythonDenyFlagsEl ? pythonDenyFlagsEl.value.split(',').map(s => s.trim()).filter(Boolean) : []
      },
      pip: {
        allow: pipAllowEl ? pipAllowEl.classList.contains('on') : true,
        require_workspace: pipRequireWsEl ? pipRequireWsEl.classList.contains('on') : false,
        allow_subcommands: pipAllowSubcommandsEl ? pipAllowSubcommandsEl.value.split(',').map(s => s.trim()).filter(Boolean) : []
      }
    };

    const schedules = {};
    container.querySelectorAll('.growth-schedule').forEach(inp => {
      if (inp.value.trim()) schedules[inp.dataset.routine] = inp.value.trim();
    });
    if (Object.keys(schedules).length > 0) updates.growth_schedules = schedules;

    const toolModels = {};
    container.querySelectorAll('.tool-model-input').forEach(inp => {
      const v = inp.value.trim();
      if (v) toolModels[inp.dataset.tmKey] = v;
    });
    updates.tool_models = toolModels;

    updates.skill_model_aliases = currentAliases;

    const chains = {};
    for (const [chainKey, state] of Object.entries(currentChains)) {
      chains[chainKey] = state.enabled;
    }
    updates.provider_chains = chains;

    const dmPolicyEl = document.getElementById('cfg-dm-policy');
    if (dmPolicyEl) updates.channel_dm_policy = dmPolicyEl.value;
    updates.channel_allowed_senders = currentAllowlist;

    const hfTokenEl = document.getElementById('cfg-hf-token');
    if (hfTokenEl && hfTokenEl.value && !hfTokenEl.value.includes('...')) {
      updates.hf_token = hfTokenEl.value.trim();
    }
    const hfClientIdEl = document.getElementById('cfg-hf-client-id');
    if (hfClientIdEl && hfClientIdEl.value.trim()) {
      updates.hf_oauth_client_id = hfClientIdEl.value.trim();
    }

    const anthropicEffortEl = document.getElementById('cfg-anthropic-effort');
    if (anthropicEffortEl) updates.anthropic_effort = anthropicEffortEl.value;

    const wakeWordsEl = document.getElementById('cfg-voice-wake-words');
    if (wakeWordsEl) updates.voice_wake_words = wakeWordsEl.value.split(',').map(s => s.trim()).filter(Boolean);
    const sttEl = document.getElementById('cfg-voice-stt');
    if (sttEl) updates.voice_stt_provider = sttEl.value;
    const chimeEl = document.getElementById('cfg-voice-chime');
    if (chimeEl) updates.voice_chime = chimeEl.checked;

    const saveRes = await api.put('/api/config', updates);
    if (saveRes && saveRes.ok === false) {
      u.toast(saveRes.error || t('config.configSaveError'), 'error');
      return;
    }

    try { await api.post('/api/autonomy/reschedule'); } catch {}

    if (updates.voice_wake_words || updates.voice_stt_provider || updates.voice_chime !== undefined || updates.voice_silence_threshold || updates.voice_silence_duration) {
      try {
        const voicePayload = {};
        if (updates.voice_wake_words) voicePayload.voice_wake_words = updates.voice_wake_words;
        if (updates.voice_stt_provider) voicePayload.voice_stt_provider = updates.voice_stt_provider;
        if (updates.voice_chime !== undefined) voicePayload.voice_chime = updates.voice_chime;
        if (updates.voice_silence_threshold) voicePayload.voice_silence_threshold = updates.voice_silence_threshold;
        if (updates.voice_silence_duration) voicePayload.voice_silence_duration = updates.voice_silence_duration;
        await api.put('/api/voice/config', voicePayload);
      } catch {}
    }

    u.toast(t('config.configSaved'));
  });

  document.getElementById('btn-reset-config')?.addEventListener('click', async () => {
    if (!confirm(t('config.resetConfirm'))) return;
    await api.put('/api/config', defs);
    u.toast(t('config.resetDefaults'));
    render(container);
  });

  document.getElementById('btn-hf-test')?.addEventListener('click', async () => {
    const tokenEl = document.getElementById('cfg-hf-token');
    const statusEl = document.getElementById('hf-token-status');
    const token = tokenEl?.value?.trim();
    if (!token || token.includes('...')) {
      statusEl.innerHTML = '<span class="text-yellow-400">Enter a token first</span>';
      return;
    }
    statusEl.innerHTML = '<span class="text-zinc-400">Testing...</span>';
    try {
      const res = await api.post('/api/nodes/hf-test', { token });
      if (res.ok) {
        statusEl.innerHTML = `<span class="text-green-400">${u.escapeHtml(res.message || 'Token valid!')}</span>`;
      } else {
        statusEl.innerHTML = `<span class="text-red-400">${u.escapeHtml(res.error || 'Token invalid')}</span>`;
      }
    } catch (e) {
      statusEl.innerHTML = `<span class="text-red-400">Test failed: ${u.escapeHtml(e.message)}</span>`;
    }
  });

  // ── HuggingFace OAuth Device Flow ───────────────────────────────
  const GHOST_HF_CLIENT_ID = 'ca3820af-dfad-4c6f-9a45-c9584be6abe1';

  document.getElementById('btn-hf-oauth')?.addEventListener('click', async () => {
    const clientIdEl = document.getElementById('cfg-hf-client-id');
    const statusEl = document.getElementById('hf-oauth-status');
    const flowEl = document.getElementById('hf-device-flow');
    const codeEl = document.getElementById('hf-device-code');
    const linkEl = document.getElementById('hf-device-link');
    const clientId = clientIdEl?.value?.trim() || GHOST_HF_CLIENT_ID;

    statusEl.innerHTML = `<span class="text-zinc-400">${t('config.hfStarting')}</span>`;

    try {
      const res = await api.post('/api/nodes/hf-device-start', { client_id: clientId });
      if (!res.ok) {
        statusEl.innerHTML = `<span class="text-red-400">${u.escapeHtml(res.error)}</span>`;
        return;
      }

      codeEl.textContent = res.user_code;
      linkEl.href = res.verification_uri;
      linkEl.textContent = res.verification_uri.replace('https://', '');
      flowEl.classList.remove('hidden');
      statusEl.innerHTML = '';

      const interval = (res.interval || 5) * 1000;
      const expiresAt = Date.now() + (res.expires_in || 900) * 1000;
      const deviceCode = res.device_code;

      const poll = async () => {
        if (Date.now() > expiresAt) {
          flowEl.classList.add('hidden');
          statusEl.innerHTML = `<span class="text-red-400">${t('config.hfExpired')}</span>`;
          return;
        }
        try {
          const pollRes = await api.post('/api/nodes/hf-device-poll', {
            device_code: deviceCode,
            client_id: clientId,
          });
          if (pollRes.ok && pollRes.status === 'authorized') {
            flowEl.classList.add('hidden');
            statusEl.innerHTML = `<span class="text-green-400">✓ ${u.escapeHtml(pollRes.message)}</span>`;
            const badge = flowEl.closest('.stat-card')?.querySelector('.rounded-full');
            if (badge) {
              badge.className = 'text-[10px] px-2 py-0.5 rounded-full bg-green-900/40 text-green-400';
              badge.textContent = t('config.hfConnected');
            }
            const hfTokenInput = document.getElementById('cfg-hf-token');
            if (hfTokenInput) hfTokenInput.value = '(set via OAuth)';
            return;
          }
          if (!pollRes.ok && pollRes.status === 'pending') {
            setTimeout(poll, interval);
            return;
          }
          flowEl.classList.add('hidden');
          statusEl.innerHTML = `<span class="text-red-400">${u.escapeHtml(pollRes.error || 'Auth failed')}</span>`;
        } catch (e) {
          setTimeout(poll, interval);
        }
      };
      setTimeout(poll, interval);
    } catch (e) {
      statusEl.innerHTML = `<span class="text-red-400">${u.escapeHtml(e.message)}</span>`;
    }
  });

  document.getElementById('btn-hf-copy-code')?.addEventListener('click', () => {
    const code = document.getElementById('hf-device-code')?.textContent;
    if (code && code !== '----') {
      navigator.clipboard.writeText(code).then(() => {
        u.toast(t('config.hfCodeCopied'));
      });
    }
  });

  // ── Quinely Reset ────────────────────────────────────────────────
  async function doReset(mode, label) {
    const statusEl = document.getElementById('reset-status');
    if (!confirm(`Are you sure you want to ${label}? A backup will be created, but this action cannot be easily undone.`)) return;
    if (mode === 'all' && !confirm('This will erase ALL Quinely data (config, memory, skills, cron, channels, evolution history). Are you absolutely sure?')) return;
    statusEl.innerHTML = '<span class="text-zinc-400">Resetting...</span>';
    try {
      const res = await api.post('/api/config/reset', { mode });
      if (res.ok) {
        statusEl.innerHTML = `<span class="text-green-400">✓ ${u.escapeHtml(res.message)}</span><br><span class="text-[10px] text-zinc-600">Backup: ${u.escapeHtml(res.backup)}</span>`;
      } else {
        statusEl.innerHTML = `<span class="text-red-400">${u.escapeHtml(res.error)}</span>`;
      }
    } catch (e) {
      statusEl.innerHTML = `<span class="text-red-400">${u.escapeHtml(e.message)}</span>`;
    }
  }

  document.getElementById('btn-reset-memory')?.addEventListener('click', () => doReset('memory', 'clear all memory'));
  document.getElementById('btn-reset-creds')?.addEventListener('click', () => doReset('config', 'reset config & credentials'));
  document.getElementById('btn-reset-all')?.addEventListener('click', () => doReset('all', 'factory reset Quinely'));

  // ── Cloud Providers ───────────────────────────────────────────
  const cpContainer = container.querySelector('#cloud-providers-container');
  const cpCostsSummary = container.querySelector('#cloud-costs-summary');

  async function loadCloudProviders() {
    try {
      const data = await api.get('/api/cloud-providers');
      const providers = data.providers || [];
      const costs = data.costs || {};

      if (cpContainer) {
        cpContainer.innerHTML = providers.map(p => {
          const statusColor = p.configured ? (p.enabled ? 'bg-emerald-500/20 text-emerald-400' : 'bg-yellow-500/20 text-yellow-400') : 'bg-zinc-700 text-zinc-500';
          const statusText = p.configured ? (p.enabled ? 'Active' : 'Configured') : 'Not configured';
          const budgetBar = p.monthly_budget_usd > 0
            ? `<div class="mt-2">
                <div class="flex justify-between text-[10px] text-zinc-500 mb-1">
                  <span>Budget</span>
                  <span>$${(p.month_spend_usd || 0).toFixed(2)} / $${p.monthly_budget_usd.toFixed(2)}</span>
                </div>
                <div class="w-full bg-surface-800 rounded-full h-1.5">
                  <div class="${(p.month_spend_usd / p.monthly_budget_usd) > 0.9 ? 'bg-red-500' : (p.month_spend_usd / p.monthly_budget_usd) > 0.7 ? 'bg-amber-500' : 'bg-ghost-500'} h-1.5 rounded-full transition-all" style="width: ${Math.min(100, (p.month_spend_usd / p.monthly_budget_usd) * 100)}%"></div>
                </div>
              </div>` : '';

          return `
            <div class="stat-card cloud-provider-card" data-provider="${u.escapeHtml(p.name)}">
              <div class="flex items-center justify-between mb-3">
                <div class="flex items-center gap-2">
                  <span class="text-lg">☁️</span>
                  <div>
                    <div class="text-sm font-semibold text-white">${u.escapeHtml(p.display_name)}</div>
                    <div class="flex gap-2 flex-wrap">
                      <a href="${u.escapeHtml(p.docs_url)}" target="_blank" rel="noopener" class="text-[10px] text-ghost-400 hover:underline">${t('config.cpApiDocs')}</a>
                      ${p.credits_url ? `<a href="${u.escapeHtml(p.credits_url)}" target="_blank" rel="noopener" class="text-[10px] text-emerald-400 hover:underline">${t('config.cpBuyCredits')}</a>` : ''}
                      ${p.keys_url ? `<a href="${u.escapeHtml(p.keys_url)}" target="_blank" rel="noopener" class="text-[10px] text-amber-400 hover:underline">${t('config.cpGetKeys')}</a>` : ''}
                    </div>
                  </div>
                </div>
                <span class="text-[10px] px-2 py-0.5 rounded-full ${statusColor}">${statusText}</span>
              </div>

              <div class="space-y-2">
                <div>
                  <label class="form-label text-[10px]">${p.needs_secret_key ? t('config.cpAccessKey') : t('config.cpApiKey')}</label>
                  <div class="flex gap-2">
                    <input type="password" class="form-input flex-1 text-xs font-mono cp-api-key" data-provider="${u.escapeHtml(p.name)}"
                           data-has-stored="${p.api_key_masked ? 'true' : 'false'}"
                           placeholder="${p.api_key_masked ? '✓ ' + t('config.cpKeyStored') : (p.needs_secret_key ? t('config.cpEnterAccessKey') : t('config.cpEnterApiKey'))}" autocomplete="off">
                    <button class="btn btn-sm btn-primary cp-test-btn" data-provider="${u.escapeHtml(p.name)}">${t('config.cpTestConnection')}</button>
                  </div>
                  <div class="cp-test-status text-[10px] mt-1" data-provider="${u.escapeHtml(p.name)}"></div>
                </div>
                ${p.needs_secret_key ? `<div>
                  <label class="form-label text-[10px]">${t('config.cpSecretKey')}</label>
                  <input type="password" class="form-input w-full text-xs font-mono cp-secret-key" data-provider="${u.escapeHtml(p.name)}"
                         data-has-stored="${p.secret_key_masked ? 'true' : 'false'}"
                         placeholder="${p.secret_key_masked ? '✓ ' + t('config.cpKeyStored') : t('config.cpEnterSecretKey')}" autocomplete="off">
                </div>` : ''}

                <div class="flex items-center justify-between py-1">
                  <span class="text-xs text-zinc-300">Enabled</span>
                  <div class="toggle cp-toggle ${p.enabled ? 'on' : ''}" data-provider="${u.escapeHtml(p.name)}"><span class="toggle-dot"></span></div>
                </div>

                <div>
                  <label class="form-label text-[10px]">Monthly Budget (USD)</label>
                  <input type="number" class="form-input w-full text-xs cp-budget" data-provider="${u.escapeHtml(p.name)}"
                         value="${p.monthly_budget_usd || 0}" min="0" max="1000" step="5" placeholder="0 = no limit">
                </div>

                ${budgetBar}

                <button class="btn btn-sm bg-surface-700 text-zinc-300 hover:bg-ghost-500/20 hover:text-ghost-400 w-full mt-2 cp-save-btn" data-provider="${u.escapeHtml(p.name)}">Save ${u.escapeHtml(p.display_name)} Settings</button>
              </div>
            </div>`;
        }).join('');

        cpContainer.querySelectorAll('.cp-toggle').forEach(toggle => {
          toggle.addEventListener('click', () => toggle.classList.toggle('on'));
        });

        cpContainer.querySelectorAll('.cp-test-btn').forEach(btn => {
          btn.addEventListener('click', async () => {
            const prov = btn.dataset.provider;
            const keyEl = cpContainer.querySelector(`.cp-api-key[data-provider="${prov}"]`);
            const secretEl = cpContainer.querySelector(`.cp-secret-key[data-provider="${prov}"]`);
            const statusEl = cpContainer.querySelector(`.cp-test-status[data-provider="${prov}"]`);
            const keyVal = keyEl?.value?.trim();
            const hasStored = keyEl?.dataset.hasStored === 'true';

            if (!keyVal && !hasStored) {
              statusEl.innerHTML = `<span class="text-yellow-400">${t('config.cpEnterKeyFirst')}</span>`;
              return;
            }

            const payload = {};
            if (keyVal) {
              payload.api_key = keyVal;
            }
            if (secretEl) {
              const secretVal = secretEl.value?.trim();
              if (secretVal) {
                payload.secret_key = secretVal;
              }
            }
            statusEl.innerHTML = `<span class="text-zinc-400">${t('config.cpTestingConnection')}</span>`;
            btn.disabled = true;
            try {
              const res = await api.post(`/api/cloud-providers/${prov}/test`, payload);
              statusEl.innerHTML = res.ok
                ? `<span class="text-green-400">${u.escapeHtml(res.message)}</span>`
                : `<span class="text-red-400">${u.escapeHtml(res.error)}</span>`;
            } catch (e) {
              statusEl.innerHTML = `<span class="text-red-400">${u.escapeHtml(e.message)}</span>`;
            }
            btn.disabled = false;
          });
        });

        cpContainer.querySelectorAll('.cp-save-btn').forEach(btn => {
          btn.addEventListener('click', async () => {
            const prov = btn.dataset.provider;
            const keyEl = cpContainer.querySelector(`.cp-api-key[data-provider="${prov}"]`);
            const secretEl = cpContainer.querySelector(`.cp-secret-key[data-provider="${prov}"]`);
            const toggleEl = cpContainer.querySelector(`.cp-toggle[data-provider="${prov}"]`);
            const budgetEl = cpContainer.querySelector(`.cp-budget[data-provider="${prov}"]`);

            const payload = {};
            const keyVal = keyEl?.value?.trim();
            if (keyVal) {
              payload.api_key = keyVal;
            }
            if (secretEl) {
              const secretVal = secretEl.value?.trim();
              if (secretVal) {
                payload.secret_key = secretVal;
              }
            }
            payload.enabled = toggleEl?.classList.contains('on') || false;
            payload.monthly_budget_usd = parseFloat(budgetEl?.value) || 0;

            btn.disabled = true;
            btn.textContent = t('config.cpSavingSettings');
            try {
              await api.put(`/api/cloud-providers/${prov}`, payload);
              u.toast(t('config.cpSettingsSaved').replace('{provider}', prov), 'success');
              loadCloudProviders();
            } catch (e) {
              u.toast(e.message, 'error');
            }
            btn.disabled = false;
            btn.textContent = t('config.cpSaveSettings').replace('{provider}', prov);
          });
        });
      }

      if (cpCostsSummary && costs.by_provider) {
        const entries = Object.entries(costs.by_provider);
        if (entries.length === 0) {
          cpCostsSummary.innerHTML = '<span class="text-zinc-500">No cloud usage this month.</span>';
        } else {
          cpCostsSummary.innerHTML = `
            <div class="flex items-center justify-between mb-2">
              <span class="text-zinc-300 font-medium">This month: $${(costs.total_usd || 0).toFixed(2)}</span>
              <span class="text-zinc-500">${costs.month}</span>
            </div>
            <div class="space-y-1">
              ${entries.map(([prov, info]) => `
                <div class="flex items-center justify-between text-[10px]">
                  <span class="text-zinc-400">${u.escapeHtml(prov)}</span>
                  <span class="text-zinc-300">$${(info.total_usd || 0).toFixed(2)} (${info.generations || 0} generations)</span>
                </div>
              `).join('')}
            </div>`;
        }
      }
    } catch (e) {
      if (cpContainer) cpContainer.innerHTML = `<div class="text-red-400 text-xs">${u.escapeHtml(e.message)}</div>`;
    }
  }

  loadCloudProviders();
}
