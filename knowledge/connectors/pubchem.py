"""
PubChem property lookup connector.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from knowledge.connectors.base import BaseConnector, RetrievedChunk

logger = logging.getLogger(__name__)


PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
DEFAULT_PROPERTIES = (
    "MolecularFormula,MolecularWeight,CanonicalSMILES,"
    "XLogP,ExactMass,TPSA,Complexity,HBondDonorCount,HBondAcceptorCount"
)
ABBREVIATION_MAP = {
    "DMAc": "N,N-Dimethylacetamide",
    "DMAC": "N,N-Dimethylacetamide",
    "DMF": "N,N-Dimethylformamide",
    "NMP": "N-Methyl-2-pyrrolidone",
    "DMSO": "Dimethyl sulfoxide",
    "THF": "Tetrahydrofuran",
    "DCM": "Dichloromethane",
    "MeCN": "Acetonitrile",
    "EtOAc": "Ethyl acetate",
    "KOAc": "Potassium acetate",
    "CsOPiv": "Cesium pivalate",
    "K2CO3": "Potassium carbonate",
    "Cs2CO3": "Cesium carbonate",
    "K3PO4": "Potassium phosphate tribasic",
    "Pd(OAc)2": "Palladium(II) acetate",
    "PdCl2": "Palladium(II) chloride",
    "Pd2(dba)3": "Tris(dibenzylideneacetone)dipalladium(0)",
    "XPhos": "2-Dicyclohexylphosphino-2',4',6'-triisopropylbiphenyl",
    "SPhos": "2-Dicyclohexylphosphino-2',6'-dimethoxybiphenyl",
    "DavePhos": "2-Dicyclohexylphosphino-2'-(N,N-dimethylamino)biphenyl",
    "PPh3": "Triphenylphosphine",
    "P(Cy)3": "Tricyclohexylphosphine",
    "dppf": "1,1'-Bis(diphenylphosphino)ferrocene",
    "BINAP": "(+/-)-2,2'-Bis(diphenylphosphino)-1,1'-binaphthyl",
}


class PubChemConnector(BaseConnector):
    """Resolve compound names or SMILES to compact property summaries."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = float(timeout)
        self._last_call_time = 0.0
        self._min_interval = 0.22
        self.last_status: dict[str, Any] = {"status": "idle", "error_type": "", "message": "", "result_count": 0}

    def is_available(self) -> bool:
        try:
            response = requests.get(
                f"{PUBCHEM_BASE}/compound/name/water/property/MolecularFormula/JSON",
                timeout=min(self.timeout, 5.0),
            )
            return response.status_code == 200
        except Exception:
            return False

    def search(self, query: str, **kwargs: Any) -> list[RetrievedChunk]:
        return self.lookup_compound(
            query,
            properties=str(kwargs.get("properties") or DEFAULT_PROPERTIES),
            fallback_smiles=str(kwargs.get("fallback_smiles") or ""),
        )

    def lookup_compound(
        self,
        name: str,
        properties: str = DEFAULT_PROPERTIES,
        fallback_smiles: str = "",
    ) -> list[RetrievedChunk]:
        name = str(name or "").strip()
        if not name:
            self.last_status = {
                "status": "available_no_result",
                "error_type": "",
                "message": "Empty compound name.",
                "result_count": 0,
            }
            return []

        expanded_name = ABBREVIATION_MAP.get(name, name)
        props = self._fetch_properties_by_name(expanded_name, properties)
        if props is None and expanded_name != name:
            props = self._fetch_properties_by_name(name, properties)
        if props is None and fallback_smiles:
            props = self._fetch_properties_by_smiles(str(fallback_smiles).strip(), properties)
        if props is None:
            logger.info("PubChem did not resolve compound '%s'.", name)
            if not self.last_status.get("status") or self.last_status.get("status") == "idle":
                self.last_status = {
                    "status": "available_no_result",
                    "error_type": "",
                    "message": f"PubChem did not resolve compound '{name}'.",
                    "result_count": 0,
                }
            return []

        cid = props.get("CID", "unknown")
        results = [
            RetrievedChunk(
                content=self._format_properties(name, props),
                source_type="pubchem",
                source_id=f"PubChem:CID_{cid}",
                metadata={
                    "cid": cid,
                    "query_name": name,
                    "resolved_name": expanded_name,
                    "molecular_formula": props.get("MolecularFormula", ""),
                    "molecular_weight": props.get("MolecularWeight", ""),
                    "smiles": props.get("CanonicalSMILES", ""),
                    "xlogp": props.get("XLogP"),
                    "tpsa": props.get("TPSA"),
                },
                query=name,
            )
        ]
        self.last_status = {
            "status": "ok",
            "error_type": "",
            "message": "",
            "result_count": len(results),
        }
        return results

    def lookup_multiple(self, names: list[str]) -> list[RetrievedChunk]:
        results: list[RetrievedChunk] = []
        for name in names:
            results.extend(self.lookup_compound(name))
        return results

    def _fetch_properties_by_name(self, name: str, properties: str) -> dict[str, Any] | None:
        self._rate_limit()
        try:
            encoded_name = requests.utils.quote(name, safe="")
            response = requests.get(
                f"{PUBCHEM_BASE}/compound/name/{encoded_name}/property/{properties}/JSON",
                timeout=self.timeout,
            )
            self._last_call_time = time.time()
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.debug("PubChem name lookup failed for '%s': %s", name, exc)
            self.last_status = {
                "status": "network_error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "result_count": 0,
            }
            return None
        items = payload.get("PropertyTable", {}).get("Properties", []) or []
        return items[0] if items else None

    def _fetch_properties_by_smiles(self, smiles: str, properties: str) -> dict[str, Any] | None:
        if not smiles:
            return None
        self._rate_limit()
        try:
            encoded = requests.utils.quote(smiles, safe="")
            response = requests.get(
                f"{PUBCHEM_BASE}/compound/smiles/{encoded}/property/{properties}/JSON",
                timeout=self.timeout,
            )
            self._last_call_time = time.time()
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.debug("PubChem SMILES lookup failed for '%s': %s", smiles, exc)
            self.last_status = {
                "status": "network_error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "result_count": 0,
            }
            return None
        items = payload.get("PropertyTable", {}).get("Properties", []) or []
        return items[0] if items else None

    @staticmethod
    def _format_properties(name: str, props: dict[str, Any]) -> str:
        parts = [f"Compound: {name}"]
        formula = props.get("MolecularFormula")
        if formula:
            parts.append(f"Molecular formula: {formula}")
        molecular_weight = props.get("MolecularWeight")
        if molecular_weight:
            parts.append(f"Molecular weight: {molecular_weight} g/mol")
        smiles = props.get("CanonicalSMILES")
        if smiles:
            parts.append(f"SMILES: {smiles}")
        xlogp = props.get("XLogP")
        if xlogp is not None:
            parts.append(f"XLogP (lipophilicity): {xlogp}")
        tpsa = props.get("TPSA")
        if tpsa is not None:
            parts.append(f"Topological polar surface area: {tpsa} Å²")
        hbd = props.get("HBondDonorCount")
        hba = props.get("HBondAcceptorCount")
        if hbd is not None and hba is not None:
            parts.append(f"H-bond donors/acceptors: {hbd}/{hba}")
        return ". ".join(parts) + "."

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_call_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
