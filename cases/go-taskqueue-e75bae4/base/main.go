package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"go-taskqueue/api"
	"go-taskqueue/queue"
	"go-taskqueue/worker"

	"github.com/joho/godotenv"
)

func main() {
	if err := godotenv.Load(); err != nil {
        log.Printf("Warning: Failed to load .env file: %v", err)
    }

	addr := os.Getenv("SERVER_ADDR")
	if addr == "" {
		addr = ":8080"
	}
	workerCount, err := strconv.Atoi(os.Getenv("WORKER_COUNT"))
	if err != nil || workerCount <= 0 {
		workerCount = 5
	}

	if err := queue.InitRedis(); err != nil {
		log.Fatalf("Failed to initialize Redis: %v", err)
	}

	worker.Init(api.TaskStore, &api.TaskStoreMu)

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup

	worker.Start(ctx, workerCount, &wg)

	server := api.NewServer(addr)

	go func() {
		log.Printf("Starting server on %s", addr)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("HTTP server error: %v", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig

	log.Println("Shutdown signal received")
	cancel()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Printf("HTTP server shutdown error: %v", err)
	}

	wg.Wait()
	log.Println("All workers stopped")
}
