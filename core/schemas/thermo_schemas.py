from pydantic import BaseModel, Field
from typing import List, Optional

class PureComponentData(BaseModel):
    component_name: str
    cas: Optional[str] = None
    equation_form: Optional[str] = None
    t_min: Optional[float] = None
    t_max: Optional[float] = None
    physical_state: Optional[str] = None
    HeatCapacityGas_coeffs: Optional[str] = Field(None, description="Comma separated or math expression")
    HeatCapacityLiquid_coeffs: Optional[str] = None
    VaporPressure_coeffs: Optional[str] = None
    VolumeLiquid_coeffs: Optional[str] = None
    ViscosityLiquid_coeffs: Optional[str] = None
    ViscosityGas_coeffs: Optional[str] = None
    ThermalConductivityLiquid_coeffs: Optional[str] = None
    ThermalConductivityGas_coeffs: Optional[str] = None
    sigma_e_coeffs: Optional[str] = None
    reference: Optional[str] = None

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
    pure_components: List[PureComponentData] = Field(default_factory=list)
    bips: List[BIPData] = Field(default_factory=list)
    enrtl: List[eNRTLData] = Field(default_factory=list)
    confidence_score: int = Field(default=0, ge=0, le=100)
    validation_notes: str = ""
