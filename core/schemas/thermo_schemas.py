from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

COEFFS_REGEX = r'^[\d\.,\-\+eE\s\*/\(\)]+$'
COEFFS_DESC = "Comma separated numerical coefficients ONLY (e.g. '1.5, -2.1e3'). DO NOT use text like 'Antoine constants'. If you only find the name of the equation but no numbers, leave this field empty/null and put the equation name in 'equation_form'."

class PureComponentData(BaseModel):
    component_name: Optional[str] = None
    id_no: Optional[str] = None
    cas: Optional[str] = None
    equation_form: Optional[str] = None
    t_min: Optional[float] = None
    t_max: Optional[float] = None
    physical_state: Optional[str] = None
    molwt: Optional[str] = Field(None, description="Molecular weight. Can be a single number.")
    tfp: Optional[str] = Field(None, description="Freezing point. Can be a single number.")
    tbp: Optional[str] = Field(None, description="Boiling point. Can be a single number.")
    tc: Optional[str] = Field(None, description="Critical temperature. Can be a single number.")
    pc: Optional[str] = Field(None, description="Critical pressure. Can be a single number.")
    vc: Optional[str] = Field(None, description="Critical volume. Can be a single number.")
    lden_coeffs: Optional[str] = Field(None, description="Liquid density. Can be a single number or coefficients.")
    tden_coeffs: Optional[str] = Field(None, description="Vapor density. Can be a single number or coefficients.")
    hvap_coeffs: Optional[str] = Field(None, description="Heat of vaporization. Can be a single number or coefficients.")
    HeatCapacityGas_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    HeatCapacityLiquid_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    VaporPressure_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    VolumeLiquid_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    ViscosityLiquid_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    ViscosityGas_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    ThermalConductivityLiquid_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    ThermalConductivityGas_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    sigma_e_coeffs: Optional[str] = Field(None, description=COEFFS_DESC)
    reference: Optional[str] = None

    @field_validator(
        'HeatCapacityGas_coeffs', 'HeatCapacityLiquid_coeffs', 'VaporPressure_coeffs',
        'VolumeLiquid_coeffs', 'ViscosityLiquid_coeffs', 'ViscosityGas_coeffs',
        'ThermalConductivityLiquid_coeffs', 'ThermalConductivityGas_coeffs', 'sigma_e_coeffs',
        'molwt', 'tfp', 'tbp', 'tc', 'pc', 'vc', 'lden_coeffs', 'tden_coeffs', 'hvap_coeffs',
        mode='before'
    )
    @classmethod
    def nullify_invalid_coeffs(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            import re
            if not re.match(COEFFS_REGEX, v):
                return None
        return v

class BIPData(BaseModel):
    component_1: str
    component_2: str
    phase: Optional[str] = None
    t_range: Optional[str] = None
    k_ij: Optional[float] = None
    A_mm: Optional[float] = None
    B_mm: Optional[float] = None
    alpha_mm: Optional[float] = None
    reference: Optional[str] = None

class eNRTLData(BaseModel):
    molecule_m: str
    cation_c: str
    anion_a: str
    t_range: Optional[str] = None
    alpha_mca: Optional[float] = None
    tau_mca_A: Optional[float] = None
    tau_mca_B: Optional[float] = None
    tau_cam_A: Optional[float] = None
    tau_cam_B: Optional[float] = None
    tau_caca: Optional[float] = None
    reference: Optional[str] = None

class ExtractionOutput(BaseModel):
    status: str = Field(default="PROCEED", description="Set to 'SKIP' if the instructions tell you to skip.")
    pure_components: List[PureComponentData] = Field(default_factory=list, description="List of pure components. MUST BE EMPTY [] if no pure components are explicitly found in the chunk.")
    bips: List[BIPData] = Field(default_factory=list, description="List of binary interaction parameters. MUST BE EMPTY [] if no BIPs are explicitly found in the chunk. Keys MUST be component_1 and component_2.")
    enrtl: List[eNRTLData] = Field(default_factory=list, description="List of eNRTL parameters. MUST BE EMPTY [] if no eNRTL parameters are explicitly found in the chunk.")
    confidence_score: int = Field(default=0, ge=0, le=100)
    validation_notes: str = ""

class ChemicalList(BaseModel):
    chemicals: List[str] = Field(default_factory=list, description="List of all chemical substances extracted from the text.")
    end_reached: bool = Field(default=False, description="Set to true if the end of the document is reached.")
