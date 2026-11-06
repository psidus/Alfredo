import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('ui/dashboard.py', 'r', encoding='utf-8', newline='') as f:
    content = f.read()

# 1. get_alfredo_robot_b64 helper at module level
helper_code = '''def get_alfredo_robot_b64() -> str:
    """Return base64 data URI of the desktop Alfredo robot icon."""
    try:
        import base64
        for candidate in ["ui/alfredo_robot.png", "ui/favicon.ico", "logo.png"]:
            if os.path.exists(candidate):
                with open(candidate, "rb") as f:
                    data = f.read()
                mime = "image/png" if candidate.endswith(".png") else "image/x-icon"
                return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except Exception:
        pass
    return ""


'''

if 'def get_alfredo_robot_b64' not in content:
    target = 'def sanitize_filename(name: str) -> str:'
    assert target in content, "sanitize_filename target not found"
    content = content.replace(target, helper_code + target, 1)

# 2. show_assistant reload & compact handling
old_show = '''    @st.dialog("🤖 Alfredo Assistant", width="large")
    def show_assistant():
        try:
            from ui.views.assistant import render_assistant

            render_assistant(compact=True)
        except Exception as exc:  # noqa: BLE001 - never break the toolbar
            st.error(f"Assistant unavailable: {exc}")
            st.exception(exc)'''

new_show = '''    @st.dialog("🤖 Alfredo Assistant", width="large")
    def show_assistant():
        try:
            import importlib
            import ui.views.assistant as assistant_mod
            importlib.reload(assistant_mod)
            render_assistant = assistant_mod.render_assistant

            try:
                render_assistant(compact=True)
            except TypeError as te:
                if "compact" in str(te):
                    render_assistant()
                else:
                    raise
        except Exception as exc:  # noqa: BLE001 - never break the toolbar
            st.error(f"Assistant unavailable: {exc}")
            st.exception(exc)'''

old_show_crlf = old_show.replace('\n', '\r\n')
new_show_crlf = new_show.replace('\n', '\r\n')
if old_show_crlf in content:
    content = content.replace(old_show_crlf, new_show_crlf, 1)
elif old_show in content:
    content = content.replace(old_show, new_show, 1)
else:
    raise AssertionError("old_show not found")

# 3. Sidebar guide button styling with desktop icon
old_sidebar_css = '''        [data-testid="stSidebar"] [data-testid="stColumn"]:has(+ [data-testid="stColumn"] .sidebar-brand-marker) .stButton > button {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0.1rem 0.2rem !important;
            min-height: auto !important;
            font-size: 1.55rem !important;
            line-height: 1 !important;
        }
        [data-testid="stSidebar"] [data-testid="stColumn"]:has(+ [data-testid="stColumn"] .sidebar-brand-marker) .stButton > button:hover {
            background: rgba(128, 128, 128, 0.12) !important;
            border-radius: 8px !important;
        }'''

new_sidebar_css = '''        [data-testid="stSidebar"] .st-key-sidebar_guide_btn button,
        [data-testid="stSidebar"] [data-testid="stColumn"]:has(+ [data-testid="stColumn"] .sidebar-brand-marker) .stButton > button {
            background-color: transparent !important;
            background-image: url('""" + get_alfredo_robot_b64() + """') !important;
            background-repeat: no-repeat !important;
            background-position: center !important;
            background-size: contain !important;
            border: none !important;
            box-shadow: none !important;
            width: 2.2rem !important;
            height: 2.2rem !important;
            min-height: 2.2rem !important;
            min-width: 2.2rem !important;
            padding: 0 !important;
            color: transparent !important;
            cursor: pointer !important;
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            transition: transform 0.15s ease, opacity 0.15s ease !important;
        }
        [data-testid="stSidebar"] .st-key-sidebar_guide_btn button:hover,
        [data-testid="stSidebar"] [data-testid="stColumn"]:has(+ [data-testid="stColumn"] .sidebar-brand-marker) .stButton > button:hover {
            opacity: 0.85 !important;
            transform: scale(1.08) !important;
            background-color: transparent !important;
            box-shadow: none !important;
            border: none !important;
        }
        [data-testid="stSidebar"] .st-key-sidebar_guide_btn button * {
            display: none !important;
            visibility: hidden !important;
        }'''

old_sidebar_crlf = old_sidebar_css.replace('\n', '\r\n')
new_sidebar_crlf = new_sidebar_css.replace('\n', '\r\n')
if old_sidebar_crlf in content:
    content = content.replace(old_sidebar_crlf, new_sidebar_crlf, 1)
elif old_sidebar_css in content:
    content = content.replace(old_sidebar_css, new_sidebar_css, 1)
else:
    raise AssertionError("old_sidebar_css not found")

# 4. Assistant robot button styling in toolbar (no circle, size of current circle)
old_toolbar_css = '''    /* Assistant: circular robot button pinned to the right end of the toolbar */
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) .st-key-assistant_open_btn {
        display: flex !important;
        justify-content: flex-end !important;
    }
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) .st-key-assistant_open_btn.st-key-assistant_open_btn button {
        width: 2.9rem !important;
        min-width: 2.9rem !important;
        height: 2.9rem !important;
        min-height: 2.9rem !important;
        padding: 0 !important;
        border-radius: 50% !important;
        font-size: 1.25rem !important;
        line-height: 1 !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
    }'''

new_toolbar_css = '''    /* Assistant: robot icon button without circle, sized to full 2.9rem */
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) .st-key-assistant_open_btn {
        display: flex !important;
        justify-content: flex-end !important;
    }
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) .st-key-assistant_open_btn.st-key-assistant_open_btn button {
        width: 2.9rem !important;
        min-width: 2.9rem !important;
        height: 2.9rem !important;
        min-height: 2.9rem !important;
        padding: 0 !important;
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        border-radius: 0 !important;
        font-size: 2.6rem !important;
        line-height: 1 !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        cursor: pointer !important;
        transition: transform 0.15s ease, opacity 0.15s ease !important;
    }
    [data-testid="stMain"] [data-testid="stHorizontalBlock"]:has(.st-key-top_bot_toggle) .st-key-assistant_open_btn.st-key-assistant_open_btn button:hover {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        transform: scale(1.1) !important;
        opacity: 0.85 !important;
    }'''

old_toolbar_crlf = old_toolbar_css.replace('\n', '\r\n')
new_toolbar_crlf = new_toolbar_css.replace('\n', '\r\n')
if old_toolbar_crlf in content:
    content = content.replace(old_toolbar_crlf, new_toolbar_crlf, 1)
elif old_toolbar_css in content:
    content = content.replace(old_toolbar_css, new_toolbar_css, 1)
else:
    raise AssertionError("old_toolbar_css not found")

# 5. Translations of Italian UI texts in dashboard.py

replacements = [
    (
        'st.error(f"Il modello \'{model_name}\' è già presente nel database.")',
        'st.error(f"Model \'{model_name}\' is already in the database.")',
    ),
    (
        'st.error(f"Errore durante l\'aggiunta del modello: {e}")',
        'st.error(f"Error adding model: {e}")',
    ),
    (
        'f"🧩 Template usato da **{len(_usage)}** workflow. "\n'
        '            "Le modifiche valgono per i nodi *collegati*; i nodi *istanza* (snapshot) "\n'
        '            "restano com\'erano finché non li sincronizzi dall\'assembler."',
        'f"🧩 Template used by **{len(_usage)}** workflow(s). "\n'
        '            "Changes apply to *linked* nodes; *instance* nodes (snapshots) "\n'
        '            "remain unchanged until you sync them in the assembler."',
    ),
    (
        'st.info("Nessun tool corrisponde alla ricerca.")',
        'st.info("No tools match your search.")',
    ),
    (
        'isolate_task = st.checkbox(\n'
        '                                "Isola come istanza (snapshot)",\n'
        '                                value=True,\n'
        '                                key="wf_single_isolate",\n'
        '                                help=(\n'
        '                                    "Copia i valori del task nel workflow: modificare la libreria "\n'
        '                                    "non cambierà più questo workflow. Potrai sincronizzare a mano."\n'
        '                                ),\n'
        '                            )',
        'isolate_task = st.checkbox(\n'
        '                                "Isolate as instance (snapshot)",\n'
        '                                value=True,\n'
        '                                key="wf_single_isolate",\n'
        '                                help=(\n'
        '                                    "Copies task values into the workflow: editing the library "\n'
        '                                    "will no longer affect this workflow. You can manually synchronize later."\n'
        '                                ),\n'
        '                            )',
    ),
    (
        'isolate_batch = st.checkbox(\n'
        '                                "Isola i task interni come istanze (snapshot)",\n'
        '                                value=True,\n'
        '                                key="wf_batch_isolate",\n'
        '                                help="Ogni task interno viene copiato nel batch: la libreria può cambiare senza effetti.",\n'
        '                            )',
        'isolate_batch = st.checkbox(\n'
        '                                "Isolate inner tasks as instances (snapshot)",\n'
        '                                value=True,\n'
        '                                key="wf_batch_isolate",\n'
        '                                help="Each inner task is copied into the batch: modifying the library will have no effect.",\n'
        '                            )',
    ),
    (
        'st.caption("🔒 istanza (snapshot del template)")',
        'st.caption("🔒 instance (template snapshot)")',
    ),
    (
        'with st.expander(f"🔒 Istanze e template ({instance_count} nodi isolati)", expanded=False):\n'
        '                    st.caption(\n'
        '                        "Un nodo **istanza** contiene una copia del task: modificare la libreria non cambia "\n'
        '                        "questo workflow. Un nodo **collegato** segue sempre il template."\n'
        '                    )',
        'with st.expander(f"🔒 Instances and templates ({instance_count} isolated nodes)", expanded=False):\n'
        '                    st.caption(\n'
        '                        "An **instance** node contains a copy of the task: editing the library does not affect "\n'
        '                        "this workflow. A **linked** node always tracks the template."\n'
        '                    )',
    ),
    (
        'st.warning(\n'
        '                            "Task collegati e condivisi con altri workflow "\n'
        '                            f"({', '.join(f\'#{t}\' for t in linked_shared)}): modificare il template "\n'
        '                            "cambierà anche questi nodi. Usa «Isola» per renderli indipendenti."\n'
        '                        )',
        'st.warning(\n'
        '                            "Tasks linked and shared with other workflows "\n'
        '                            f"({', '.join(f\'#{t}\' for t in linked_shared)}): modifying the template "\n'
        '                            "will affect these nodes too. Use \'Isolate\' to make them independent."\n'
        '                        )',
    ),
    (
        'if st.button(\n'
        '                        "Isola i task condivisi di questo workflow",\n'
        '                        key="wf_isolate_shared",\n'
        '                        help="Crea uno snapshot per i task usati da più di un workflow.",\n'
        '                    ):\n'
        '                        try:\n'
        '                            st.session_state.wf_selected_task_ids = isolate_workflow_nodes(\n'
        '                                st.session_state.wf_selected_task_ids, db, only_shared=True\n'
        '                            )\n'
        '                            st.rerun()\n'
        '                        except Exception as exc:\n'
        '                            st.error(f"Isolamento non riuscito: {exc}")',
        'if st.button(\n'
        '                        "Isolate shared tasks in this workflow",\n'
        '                        key="wf_isolate_shared",\n'
        '                        help="Create a snapshot for tasks used by more than one workflow.",\n'
        '                    ):\n'
        '                        try:\n'
        '                            st.session_state.wf_selected_task_ids = isolate_workflow_nodes(\n'
        '                                st.session_state.wf_selected_task_ids, db, only_shared=True\n'
        '                            )\n'
        '                            st.rerun()\n'
        '                        except Exception as exc:\n'
        '                            st.error(f"Isolation failed: {exc}")',
    ),
    (
        'f"usato in {len(used_by)} workflow"',
        'f"used in {len(used_by)} workflow(s)"',
    ),
    (
        'st.caption("Template non trovato: l\'istanza continua a funzionare da sola.")',
        'st.caption("Template not found: instance continues running standalone.")',
    ),
    (
        'st.caption("✅ Allineata al template.")',
        'st.caption("✅ Synced with template.")',
    ),
    (
        'st.caption("Differenze: " + ", ".join(sorted(diff.keys())))',
        'st.caption("Differences: " + ", ".join(sorted(diff.keys())))',
    ),
    (
        '"Campi da sincronizzare"',
        '"Fields to synchronize"',
    ),
    (
        '"Sincronizza scelti"',
        '"Sync selected"',
    ),
    (
        '"Sincronizza tutto"',
        '"Sync all"',
    ),
    (
        '"Scollega"',
        '"Detach"',
    ),
    (
        'help="Torna a puntare al template (perde la copia locale)."',
        'help="Revert to pointing to the template (local copy will be lost)."',
    ),
    (
        'st.info(\n'
        '                            "Nessuna istanza in questo workflow: ogni nodo segue il template della "\n'
        '                            "libreria. Usa «Isola» per rendere il workflow indipendente."\n'
        '                        )',
        'st.info(\n'
        '                            "No instances in this workflow: every node follows the library "\n'
        '                            "template. Use \'Isolate\' to make the workflow independent."\n'
        '                        )',
    ),
    (
        'st.caption(f"Calendario non disponibile: {e}")',
        'st.caption(f"Calendar unavailable: {e}")',
    ),
    (
        'st.caption(\n'
        '                        "Nessuna esecuzione programmata nelle prossime 48 ore "\n'
        '                        "(i folder-watch sono event-driven e non compaiono qui)."\n'
        '                    )',
        'st.caption(\n'
        '                        "No runs scheduled in the next 48 hours "\n'
        '                        "(folder-watch tasks are event-driven and do not appear here)."\n'
        '                    )',
    ),
    (
        'st.markdown(\n                        "| Quando | Regola | Workflow | Tipo |\n|---|---|---|---|\n" + table_rows\n                    )',
        'st.markdown(\n                        "| When | Rule | Workflow | Type |\n|---|---|---|---|\n" + table_rows\n                    )',
    ),
    (
        'help="Modifica"',
        'help="Edit"',
    ),
    (
        'st.markdown(f"##### ✏️ Modifica regola: {editing_rule.get(\'name\')}")',
        'st.markdown(f"##### ✏️ Edit rule: {editing_rule.get(\'name\')}")',
    ),
    (
        'e_name = ec1.text_input("Nome", value=editing_rule.get("name", ""))',
        'e_name = ec1.text_input("Name", value=editing_rule.get("name", ""))',
    ),
    (
        'e_type = ec3.selectbox("Tipo", type_options, index=type_index)',
        'e_type = ec3.selectbox("Type", type_options, index=type_index)',
    ),
    (
        'e_param = ec4.text_input("Parametro", value=param_default)',
        'e_param = ec4.text_input("Parameter", value=param_default)',
    ),
    (
        'e_catchup = ec5.number_input(\n                        "Catch-up (minuti)",',
        'e_catchup = ec5.number_input(\n                        "Catch-up (minutes)",',
    ),
    (
        'help="Finestra entro cui un trigger mancato viene recuperato. Oltre, viene saltato."',
        'help="Window within which a missed trigger is caught up. Beyond this, it is skipped."',
    ),
    (
        'e_enabled = ec6.checkbox("Attiva", value=bool(editing_rule.get("enabled", True)))',
        'e_enabled = ec6.checkbox("Enabled", value=bool(editing_rule.get("enabled", True)))',
    ),
    (
        'save_rule = save_col.form_submit_button("💾 Salva", type="primary", use_container_width=True)',
        'save_rule = save_col.form_submit_button("💾 Save", type="primary", use_container_width=True)',
    ),
    (
        'close_rule = cancel_col.form_submit_button("✖ Chiudi", use_container_width=True)',
        'close_rule = cancel_col.form_submit_button("✖ Close", use_container_width=True)',
    ),
    (
        'st.toast("Regola aggiornata", icon="✅")',
        'st.toast("Rule updated", icon="✅")',
    ),
    (
        'if st.button("Genera Dataset ChatML", type="primary"):',
        'if st.button("Generate ChatML Dataset", type="primary"):',
    ),
    (
        'st.info("Salvataggio file e conversione in ChatML...")',
        'st.info("Saving file and converting to ChatML...")',
    ),
    (
        'f"✅ Dataset generato: `{result[\'path\']}` — "\n                        f"{result[\'total_entries\']} esempi da {len(result[\'processed_files\'])} file."',
        'f"✅ Dataset generated: `{result[\'path\']}` — "\n                        f"{result[\'total_entries\']} example(s) from {len(result[\'processed_files\'])} file(s)."',
    ),
    (
        'st.error("Nessun esempio generato. Controlla i file e i messaggi sotto.")',
        'st.error("No examples generated. Check files and messages below.")',
    ),
    (
        'st.warning("Carica almeno un file prima.")',
        'st.warning("Upload at least one file first.")',
    ),
    (
        'st.caption(f"Dataset pronto: **{ds_count}** esempi — `{ds_msg}`")',
        'st.caption(f"Dataset ready: **{ds_count}** examples — `{ds_msg}`")',
    ),
    (
        'st.info("Dataset presente ma anteprima vuota.")',
        'st.info("Dataset present but preview is empty.")',
    ),
    (
        'st.caption(\n                    "Llama gated: imposta `HUGGINGFACE_TOKEN` nel `.env` e accetta la licenza su HuggingFace."\n                )',
        'st.caption(\n                    "Llama gated: set `HUGGINGFACE_TOKEN` in `.env` and accept the license on HuggingFace."\n                )',
    ),
    (
        'st.info(f"Dataset: {ds_count} esempi pronti.")',
        'st.info(f"Dataset: {ds_count} examples ready.")',
    ),
    (
        'if st.button("Avvia Addestramento 🚀", type="primary", use_container_width=True):',
        'if st.button("Start Training 🚀", type="primary", use_container_width=True):',
    ),
    (
        'st.info("Apri il tab Monitoring per seguire progressi e log.")',
        'st.info("Open the Monitoring tab to track progress and logs.")',
    ),
    (
        'st.error(result.get("message", "Avvio training fallito."))',
        'st.error(result.get("message", "Training startup failed."))',
    ),
    (
        'label += " ✅ Completato"',
        'label += " ✅ Completed"',
    ),
    (
        'label += " ⏳ In corso"',
        'label += " ⏳ In progress"',
    ),
    (
        'if st.button("Aggiorna ora"):',
        'if st.button("Refresh now"):',
    ),
    (
        'st.info("In attesa dei primi valori di loss...")',
        'st.info("Waiting for first loss values...")',
    ),
    (
        'st.caption("Nessuna curva loss ancora. Avvia un training per vedere i dati reali.")',
        'st.caption("No loss curve yet. Start training to see real data.")',
    ),
    (
        'st.markdown("Testa l\'adapter LoRA salvato in `storage/adapters/temp` dopo il training.")',
        'st.markdown("Test the LoRA adapter saved in `storage/adapters/temp` after training.")',
    ),
    (
        'st.warning("Nessun adapter trovato. Completa prima un training.")',
        'st.warning("No adapter found. Complete training first.")',
    ),
    (
        'prompt = st.chat_input("Di\' qualcosa al modello fine-tuned...")',
        'prompt = st.chat_input("Say something to the fine-tuned model...")',
    ),
    (
        'with st.spinner("Generazione in corso..."):',
        'with st.spinner("Generating..."):',
    ),
    (
        'st.error(f"Errore nel caricamento di API Vault & Model Registry: {e}")',
        'st.error(f"Error loading API Vault & Model Registry: {e}")',
    ),
    (
        'st.error(f"Errore nel caricamento del Database: {e}")',
        'st.error(f"Error loading Database: {e}")',
    ),
    (
        'st.error(f"Errore nel caricamento di Agent Caserma: {e}")',
        'st.error(f"Error loading Agent Caserma: {e}")',
    ),
    (
        'st.error(f"Errore nel caricamento di Tool Factory: {e}")',
        'st.error(f"Error loading Tool Factory: {e}")',
    ),
    (
        'st.error(f"Errore nel caricamento di Task Builder: {e}")',
        'st.error(f"Error loading Task Builder: {e}")',
    ),
    (
        'st.error(f"Errore nel caricamento di Workflow Assembler: {e}")',
        'st.error(f"Error loading Workflow Assembler: {e}")',
    ),
    (
        'st.error(f"Errore nel caricamento di History & Monitoring: {e}")',
        'st.error(f"Error loading History & Monitoring: {e}")',
    ),
]

for old, new in replacements:
    old_c = old.replace('\n', '\r\n')
    new_c = new.replace('\n', '\r\n')
    if old_c in content:
        content = content.replace(old_c, new_c, 1)
        print("Replaced:", repr(old[:25]))
    elif old in content:
        content = content.replace(old, new, 1)
        print("Replaced:", repr(old[:25]))
    else:
        print("Warning: NOT found:", repr(old[:25]))

with open('ui/dashboard.py', 'w', encoding='utf-8', newline='') as f:
    f.write(content)

print("Finished updating ui/dashboard.py")
