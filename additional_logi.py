# --- LOGICHE PERSONALIZZATE PER WORKFLOW (Scalabile e Flessibile) ---
async def prompt_for_specific_data(update, context, prompt_text):
    """Utility per chiedere un dato specifico e restare in attesa."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=prompt_text,
        parse_mode=ParseMode.HTML
    )
    return CAPTURE_EDITS
# Mappa dei workflow che richiedono controlli speciali
# Chiave: nome del workflow (o ID), Valore: funzione di controllo
WORKFLOW_CUSTOM_LOGIC = {
    "brainstorming": {
        "required_keys": ["idea", "target"],
        "prompt": "Ho bisogno di un'idea e del target di riferimento. Scrivili qui:"
    },
    "report_finanziario": {
        "required_keys": ["ticker_azienda"],
        "prompt": "Per quale azienda vuoi il report? Inserisci il ticker (es: AAPL, TSLA):"
    }
}
async def validate_workflow_requirements(update: Update, context: ContextTypes.DEFAULT_TYPE, workflow_name: str):
    """
    Controlla se il workflow ha bisogno di input specifici prima di partire.
    Ritorna True se tutto ok, False se bisogna fermarsi a chiedere.
    """
    wf_key = workflow_name.lower()
    
    # 1. Cerca se ci sono regole per questo workflow
    for key, config in WORKFLOW_CUSTOM_LOGIC.items():
        if key in wf_key:
            # Controlla se abbiamo già i dati necessari nell'execution_context
            current_context = context.user_data.get("execution_context", {})
            missing = [k for k in config["required_keys"] if k not in current_context]
            
            if missing:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⚠️ <b>Dati mancanti per {workflow_name}:</b>\n{config['prompt']}",
                    parse_mode=ParseMode.HTML
                )
                return False # Ferma l'esecuzione e aspetta input
                
    return True # Tutto pronto per la Crew