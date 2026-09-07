"""Small shared errors for read-only committed Recipe projections."""


class PipelineRecipeNotFoundError(Exception):
    """The requested durable pipeline run does not exist for Recipe inspection."""
