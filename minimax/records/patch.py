    def filter_by_tag(self, tag: str) -> tuple[Task, ...]:
        normalized = tag.strip().lower()
        if not normalized:
            return ()
        return tuple(
            task for task in self._tasks.values() if normalized in task.tags
        )
