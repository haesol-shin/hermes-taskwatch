"""Run callables at the same time and return what each returned, in order."""

import threading


def in_threads(jobs):
    results = [None] * len(jobs)

    def run(index, job):
        results[index] = job()

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results
