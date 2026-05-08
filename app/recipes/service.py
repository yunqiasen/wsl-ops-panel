from pathlib import Path

from app.models.recipes import DockerRecipe
from app.recipes.loader import load_docker_recipes


class DockerRecipeService:
    def __init__(self, config_root: Path | str) -> None:
        self._config_root = Path(config_root)
        self._recipes = load_docker_recipes(self._config_root / 'recipes' / 'docker')

    def get(self, recipe_id: str | None) -> DockerRecipe | None:
        if recipe_id is None:
            return None
        recipe = self._recipes.get(recipe_id)
        return recipe.model_copy(deep=True) if recipe is not None else None
