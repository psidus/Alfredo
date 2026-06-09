from pydantic import BaseModel, Field
from typing import Optional

class ChimicaComponent(BaseModel):
    """
    Classe "Scheletro" per l'estrazione di dati chimici dai documenti.
    Viene utilizzata dagli agenti per validare l'output (Structured Output) 
    e passarlo alla pipeline del Database PostgreSQL.
    """
    
    # Esempi di campi citati dall'utente. 
    # Field(...) serve a dare all'LLM una descrizione chiara di cosa estrarre.
    
    idrogeno: Optional[float] = Field(
        default=None,
        description="La quantità, concentrazione o percentuale di Idrogeno rilevata."
    )
    
    PM: Optional[float] = Field(
        default=None,
        description="Il Peso Molecolare (PM) del composto, se presente nel testo."
    )
    
    Cs: Optional[float] = Field(
        default=None,
        description="La quantità, concentrazione o percentuale di Cesio (Cs) rilevata."
    )
    
    nome_composto: str = Field(
        description="Il nome del composto chimico o dell'elemento principale analizzato."
    )
    
    # Aggiungi qui gli altri vari campi che dovranno essere cercati nel database.
    # ...

    class Config:
        # Se intendi passare l'oggetto a un ORM come SQLAlchemy,
        # questo permette di serializzare l'oggetto più facilmente.
        from_attributes = True

