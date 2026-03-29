from pydantic import BaseModel


class AffinityGenerateResult(BaseModel):
    customers_processed: int
    affinities_upserted: int
